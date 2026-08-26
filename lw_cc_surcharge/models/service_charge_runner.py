# -*- coding: utf-8 -*-
"""Monthly cron runner for service charges on past-due AR.

AbstractModel called by
``ir.cron_monthly_service_charge``. Ships DISABLED;  enables
after counsel sign-off.

Flow per cron tick:

1. For each company with ``lw_cc_surcharge_enabled=True``:
   a. Scan posted ``out_invoice`` where:
      - ``payment_state IN ('not_paid', 'partial')``
      - ``amount_residual > 0``
      - ``invoice_date_due < today - past_due_days``
      - ``invoice_payment_term_id`` IN applicable terms (if configured)
   b. Exclude invoices with ``dunning_hold=True`` (invoice-level
      hold; disputed-invoice protection).
   c. Group by ``commercial_partner_id``.
   d. For each partner:
      - Skip if the commercial partner is exempt via
        ``lw_cc_service_charge_optout`` (default off: everyone is
        subject to the charge unless deliberately exempted).
      - Sum residuals of qualifying invoices (min_balance is always a
        PARTNER-level gate, even in 'invoice_line' mode below).
      - Skip if sum < ``lw_cc_surcharge_min_balance``.
      - Branch on ``company.lw_cc_sc_mode``:
        * 'separate_invoice' (default): skip if ANY of the partner's
          posted out_invoices (not just this run's eligible set --
          see the note on ``_process_partner``) already carry this
          month's ``lw_cc_sc_last_assessed_month`` (the symmetric
          cross-mode guard -- protects the designed rollback path when
          an operator flips ``lw_cc_sc_mode`` back from 'invoice_line'
          mid-month); otherwise calculate charge = sum *
          (``lw_cc_surcharge_pct`` / 100); if dry-run, log the
          computed charge; if live, create + post one charge invoice
          via ``account.move._create_service_charge_invoice()``.
        * 'invoice_line': assess interest as a line on EACH qualifying
          invoice individually (own nested savepoint per invoice), via
          ``_process_partner_invoice_line`` /
          ``account.move._lw_cc_add_interest_line()``. Base amount per
          ``lw_cc_sc_compounding`` ('simple' subtracts prior interest
          lines first; 'compound' uses the full residual). Partial
          invoices follow ``lw_cc_sc_partial_policy``.
   e. Log per-company summary.
   f. Write ONE ``lw_cc.service.charge.run`` audit record (header +
      per-invoice detail lines) for the whole company pass.

Safety:
  - Per-partner ``env.cr.savepoint()`` isolation: one failure does not
    roll back other partners' charge invoices. In 'invoice_line' mode,
    each invoice ALSO gets its own nested savepoint, so one locked or
    hash-locked invoice cannot roll back a partner's other invoices.
  - Never raises at cron level: all exceptions caught and logged.

Run records :
  ``_process_partner`` and ``_process_partner_invoice_line`` return
  their existing stats dicts ENRICHED with ``'reason'`` (the
  partner-level outcome) and ``'lines'`` (plain dicts describing the
  per-invoice detail). Their signatures are unchanged, so existing
  callers and tests keep working. ``_run_for_company`` accumulates
  those dicts in memory and creates the run record ONCE, AFTER the
  partner loop, OUTSIDE every savepoint.

  That placement is not incidental. Writing a run line inside the
  per-partner savepoint would mean a rollback erases exactly the audit
  row that explains why the partner failed -- the report would be
  silent about its most interesting case. The exception handler in the
  partner loop therefore contributes its own line
  (reason='unexpected_error') from outside the rolled-back block.

  Run records are AUDIT objects, not accounting objects: they are
  written in dry-run mode too. Dry-run still creates zero
  ``account.move`` / ``account.move.line`` rows and still posts zero
  chatter.
"""
import logging
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare, float_round

_logger = logging.getLogger(__name__)


class ServiceChargeRunner(models.AbstractModel):
    _name = 'lw_cc.service.charge.runner'
    _description = 'Monthly Service Charge on Past-Due AR Runner'

    # ------------------------------------------------------------------
    # Cron entry point
    # ------------------------------------------------------------------

    @api.model
    def _run_monthly_service_charge(self):
        """Monthly cron entry point. Iterates companies, computes and
        applies service charges on past-due AR.

        Never raises at cron level.

        :returns: list of ``lw_cc.service.charge.run`` ids created by
            this tick (one per company that was processed). The cron
            ignores the return value; it exists so an operator running
            the method by hand from ``odoo-bin shell`` gets the report
            ids back directly instead of grepping the log. The ids are
            logged as well.
        """
        Company = self.env['res.company'].sudo()
        today = fields.Date.context_today(self)
        companies = Company.search([
            ('lw_cc_surcharge_enabled', '=', True),
        ])

        if not companies:
            _logger.info(
                "lw_cc_surcharge: service charge cron tick; no "
                "companies have the feature enabled. Nothing to do."
            )
            return []

        _logger.info(
            "lw_cc_surcharge: service charge cron tick; today=%s; "
            "%d enabled company/companies.",
            today, len(companies),
        )

        grand_total_charged = 0.0
        grand_partners_charged = 0
        grand_partners_skipped = 0
        grand_invoices_charged = 0
        grand_skipped_locked = 0
        grand_skipped_partial = 0
        grand_skipped_hashed = 0
        grand_skipped_other = 0
        run_ids = []

        for company in companies:
            try:
                stats = self.with_company(company)._run_for_company(
                    company, today,
                )
                if stats.get('run_id'):
                    run_ids.append(stats['run_id'])
                grand_total_charged += stats['charged_total']
                grand_partners_charged += stats['partners_charged']
                grand_partners_skipped += stats['partners_skipped']
                grand_invoices_charged += stats.get('invoices_charged', 0)
                grand_skipped_locked += stats.get('skipped_locked', 0)
                grand_skipped_partial += stats.get('skipped_partial', 0)
                grand_skipped_hashed += stats.get('skipped_hashed', 0)
                grand_skipped_other += stats.get('skipped_other', 0)
            except Exception:  # noqa: BLE001
                _logger.exception(
                    "lw_cc_surcharge: unhandled error while running "
                    "service charge for company %s; continuing with "
                    "next company.",
                    company.display_name,
                )

        _logger.info(
            "lw_cc_surcharge: cron tick complete. "
            "Partners charged: %d, skipped: %d. "
            "Total charged: %.2f (across %d companies). "
            "invoice_line mode: %d invoices charged, "
            "skipped_locked=%d skipped_partial=%d skipped_hashed=%d "
            "skipped_other=%d. Run record(s): %s.",
            grand_partners_charged, grand_partners_skipped,
            grand_total_charged, len(companies),
            grand_invoices_charged, grand_skipped_locked,
            grand_skipped_partial, grand_skipped_hashed, grand_skipped_other,
            run_ids or "none created",
        )
        return run_ids

    # ------------------------------------------------------------------
    # Per-company processing
    # ------------------------------------------------------------------

    def _run_for_company(self, company, today):
        """Process a single company. Returns a stats dict.

        Also writes ONE ``lw_cc.service.charge.run`` audit record (header
        plus detail lines) for the whole pass, after the partner loop and
        outside every savepoint -- see this module's docstring for why
        that placement is load-bearing.

        :returns: dict with keys:
            - ``partners_charged``: int
            - ``partners_skipped``: int
            - ``charged_total``: float
            - ``dry_run``: bool
            - ``run_id``: int id of the run record, or False if the
              audit write itself failed (which never aborts the run)
        """
        dry_run = company.lw_cc_surcharge_dry_run
        pct = company.lw_cc_surcharge_pct or 0.0
        min_balance = company.lw_cc_surcharge_min_balance or 0.0
        past_due_days = company.lw_cc_surcharge_past_due_days or 30
        due_cutoff = today - timedelta(days=past_due_days)

        # Applicable terms filter (Net 30+). If no terms configured,
        # apply to all posted invoices (Terms are used for CC surcharge
        # gating; for service charge, all past-due invoices
        # qualify regardless of terms, since the legal basis is the
        # published terms language).
        applicable_term_ids = company.lw_cc_surcharge_applicable_term_ids.ids

        Move = self.env['account.move'].with_company(company).sudo()
        domain = [
            ('company_id', '=', company.id),
            ('move_type', '=', 'out_invoice'),
            ('state', '=', 'posted'),
            ('payment_state', 'in', ['not_paid', 'partial']),
            ('amount_residual', '>', 0),
            ('invoice_date_due', '!=', False),
            ('invoice_date_due', '<', due_cutoff),
        ]

        # Conditionally exclude invoices with dunning_hold. This field
        # is added by an AR dunning module; if it is not installed the
        # field does not exist and the domain would crash.
        if 'dunning_hold' in self.env['account.move']._fields:
            domain.append(('dunning_hold', '=', False))

        # Only filter by terms if explicitly configured. This lets the
        # service charge apply to all past-due customers by default
        # (the terms language is universal per the published terms).
        if applicable_term_ids:
            domain.append(
                ('invoice_payment_term_id', 'in', applicable_term_ids),
            )

        eligible = Move.search(domain)

        if not eligible:
            _logger.info(
                "lw_cc_surcharge: company %s has no eligible "
                "past-due invoices (cutoff due date < %s).",
                company.display_name, due_cutoff,
            )
            stats = {
                'partners_charged': 0,
                'partners_skipped': 0,
                'charged_total': 0.0,
                'dry_run': dry_run,
                'invoices_charged': 0,
                'skipped_locked': 0,
                'skipped_partial': 0,
                'skipped_hashed': 0,
                'skipped_other': 0,
            }
            # An empty run is still worth recording: "the cron ran and
            # found nothing" and "the cron never ran" look identical
            # otherwise, and the difference is the whole point of the
            # dry-run-armed posture.
            stats['run_id'] = self._create_run_record(
                company, today, dry_run, pct, stats, [],
                note=(
                    "No eligible past-due invoices (due date earlier "
                    "than %s). Nothing computed." % due_cutoff
                ),
            )
            return stats

        # Group by commercial partner.
        by_partner = {}
        for move in eligible:
            by_partner.setdefault(
                move.commercial_partner_id.id, []
            ).append(move.id)

        _logger.info(
            "lw_cc_surcharge: company %s: %d eligible past-due "
            "invoices across %d commercial partners "
            "(dry_run=%s, pct=%.2f%%, min_balance=%.2f).",
            company.display_name, len(eligible), len(by_partner),
            dry_run, pct, min_balance,
        )

        partners_charged = 0
        partners_skipped = 0
        charged_total = 0.0
        # 'invoice_line' mode stats. Always 0 for partners
        # processed in 'separate_invoice' mode, since that mode charges
        # or skips the whole partner, not individual invoices.
        invoices_charged = 0
        skipped_locked = 0
        skipped_partial = 0
        skipped_hashed = 0
        skipped_other = 0
        # Accumulated in memory during the loop; the run record is
        # created from it AFTER the loop, outside every savepoint.
        run_lines = []

        for partner_id, move_ids in by_partner.items():
            partner = self.env['res.partner'].sudo().browse(partner_id)
            moves = Move.browse(move_ids)
            # Read BEFORE the savepoint, off the prefetched recordset.
            # The failure path below needs this figure for its audit
            # row, and by then the savepoint's rollback has called
            # cr.clear(), so reading it there would mean re-querying
            # every one of the partner's invoices on the error path.
            # The value is the same either way (the rollback restores
            # the pre-run state); this just gets it for free.
            partner_residual = sum(moves.mapped('amount_residual'))

            try:
                with self.env.cr.savepoint():
                    result = self._process_partner(
                        partner, moves, company, today,
                        pct, min_balance, dry_run,
                    )
            except Exception as exc:  # noqa: BLE001
                _logger.exception(
                    "lw_cc_surcharge: per-partner processing failed "
                    "for partner=%s company=%s; savepoint rolled back, "
                    "continuing.",
                    partner.display_name, company.display_name,
                )
                partners_skipped += 1
                # Contributed from OUT HERE, not from inside the
                # savepoint: a failure that rolls back must still leave
                # a trace in the report, or the one partner worth
                # investigating is the one the report omits.
                run_lines.append({
                    'partner_id': partner_id,
                    'move_id': False,
                    'amount_residual': partner_residual,
                    'computed_interest': 0.0,
                    'action': 'skipped',
                    'reason': 'unexpected_error',
                    'reason_detail': (
                        str(exc) or exc.__class__.__name__
                    )[:512],
                })
                continue

            for line_vals in result.get('lines') or []:
                line_vals.setdefault('partner_id', partner_id)
                run_lines.append(line_vals)

            if result.get('charged'):
                partners_charged += 1
                charged_total += result['charge_amount']
            else:
                partners_skipped += 1

            invoices_charged += result.get('invoices_charged', 0)
            skipped_locked += result.get('skipped_locked', 0)
            skipped_partial += result.get('skipped_partial', 0)
            skipped_hashed += result.get('skipped_hashed', 0)
            skipped_other += result.get('skipped_other', 0)

        mode_label = 'DRY-RUN' if dry_run else 'LIVE'
        _logger.info(
            "lw_cc_surcharge: company %s [%s]: charged %d partners "
            "(%.2f total), skipped %d partners. invoice_line mode: "
            "%d invoices charged, skipped_locked=%d skipped_partial=%d "
            "skipped_hashed=%d skipped_other=%d.",
            company.display_name, mode_label,
            partners_charged, charged_total, partners_skipped,
            invoices_charged, skipped_locked, skipped_partial,
            skipped_hashed, skipped_other,
        )

        stats = {
            'partners_charged': partners_charged,
            'partners_skipped': partners_skipped,
            'charged_total': charged_total,
            'dry_run': dry_run,
            'invoices_charged': invoices_charged,
            'skipped_locked': skipped_locked,
            'skipped_partial': skipped_partial,
            'skipped_hashed': skipped_hashed,
            'skipped_other': skipped_other,
        }
        stats['run_id'] = self._create_run_record(
            company, today, dry_run, pct, stats, run_lines,
            note=(
                "%d past-due invoice(s) across %d commercial partner(s) "
                "scanned; %d partner(s) charged, %d skipped. "
                "Delivery mode: %s. Rate: %.2f%%. Minimum past-due "
                "balance: %.2f. Grace: %d day(s)." % (
                    len(eligible), len(by_partner),
                    partners_charged, partners_skipped,
                    company.lw_cc_sc_mode or 'separate_invoice',
                    pct, min_balance, past_due_days,
                )
            ),
        )
        return stats

    # ------------------------------------------------------------------
    # Run record creation (audit; never accounting)
    # ------------------------------------------------------------------

    def _create_run_record(
        self, company, today, dry_run, pct, stats, run_lines, note='',
    ):
        """Write the ``lw_cc.service.charge.run`` header + detail lines.

        Called ONCE per company pass, after the partner loop, outside
        every savepoint.

        Never raises. The audit record must not be able to break a cron
        tick, and above all must not roll back charge invoices that were
        already created and posted in live mode: a report that failed to
        write is a nuisance, an accounting write that unwound because of
        a report bug is a real problem.

        :returns: the created run's id, or False if the write failed.
        """
        mode = 'dry_run' if dry_run else 'live'
        # Invariant: every partner in the loop increments exactly one of
        # the two counters (the exception path increments skipped and
        # continues), so their sum is the number of partners scanned.
        partner_count = (
            stats['partners_charged'] + stats['partners_skipped']
        )
        has_error = any(
            vals.get('reason') == 'unexpected_error' for vals in run_lines
        )
        try:
            # Its own savepoint. In live mode this runs AFTER charge
            # invoices have been created and posted in this same
            # transaction, and a failed statement poisons a PostgreSQL
            # transaction outright ("current transaction is aborted"),
            # which would take those invoices down with it at commit.
            # A savepoint here means the worst case is a lost report,
            # never lost accounting. It is NOT the per-partner savepoint
            # the run lines are deliberately kept out of; this one wraps
            # the audit write alone.
            with self.env.cr.savepoint():
                run = self.env['lw_cc.service.charge.run'].sudo().create({
                    'name': "SC Run %s [%s]" % (
                        today, 'DRY-RUN' if dry_run else 'LIVE',
                    ),
                    'run_date': fields.Datetime.now(),
                    'company_id': company.id,
                    'mode': mode,
                    'sc_mode': company.lw_cc_sc_mode or 'separate_invoice',
                    'pct': pct,
                    'partner_count': partner_count,
                    'charged_partner_count': stats['partners_charged'],
                    'skipped_partner_count': stats['partners_skipped'],
                    'total_computed': stats['charged_total'],
                    'state': 'error' if has_error else 'done',
                    'note': note,
                })
                if run_lines:
                    self.env[
                        'lw_cc.service.charge.run.line'
                    ].sudo().create([{
                        'run_id': run.id,
                        'partner_id': vals['partner_id'],
                        'move_id': vals.get('move_id') or False,
                        'amount_residual': (
                            vals.get('amount_residual') or 0.0
                        ),
                        'computed_interest': (
                            vals.get('computed_interest') or 0.0
                        ),
                        'action': vals['action'],
                        'reason': vals['reason'],
                        'reason_detail': vals.get('reason_detail') or False,
                    } for vals in run_lines])
            _logger.info(
                "lw_cc_surcharge: wrote service charge run record "
                "%s (id=%d) for company %s with %d detail line(s).",
                run.name, run.id, company.display_name, len(run_lines),
            )
            return run.id
        except Exception:  # noqa: BLE001
            _logger.exception(
                "lw_cc_surcharge: could not write the service charge "
                "run record for company %s; the run itself completed and "
                "any charges it created stand. %d detail line(s) lost.",
                company.display_name, len(run_lines),
            )
            return False

    # ------------------------------------------------------------------
    # Run line helpers
    # ------------------------------------------------------------------

    def _suppression_line(self, moves, reason, reason_detail=''):
        """A partner-level run line: the whole partner was suppressed
        before any individual invoice was considered.

        ``move_id`` is deliberately empty -- no single invoice caused
        this -- and ``amount_residual`` carries the partner's total
        past-due residual so the report still shows how much money the
        suppression covered. ``computed_interest`` stays 0.00: nothing
        was charged, and this column's sum must keep matching the run
        header's Total Computed.
        """
        return {
            'move_id': False,
            'amount_residual': sum(moves.mapped('amount_residual')),
            'computed_interest': 0.0,
            'action': 'skipped',
            'reason': reason,
            'reason_detail': reason_detail,
        }

    def _split_charge_across_moves(self, moves, charge_amount, company, pct):
        """Pro-rata split of a partner-level charge across its source
        invoices.

        'separate_invoice' mode computes ONE lump-sum charge from the
        partner's total residual, but the report has to answer "which
        invoices have the most interest accrued", so the lump sum is
        attributed back to the invoices that generated it: each invoice
        gets ``residual * pct/100``.

        The parts are individually rounded, so they can miss the
        already-rounded partner total by a cent or two. The drift is
        assigned to the largest invoice, which makes the detail lines
        sum EXACTLY to the charge invoice's amount. Without that, 's
        report would disagree with the invoice it describes by a few
        cents and every reconciliation of the two would start with
        finding out why.

        :returns: list of floats, parallel to ``moves``.
        """
        rounding = company.currency_id.rounding
        shares = [
            float_round(
                move.amount_residual * (pct / 100.0),
                precision_rounding=rounding,
            )
            for move in moves
        ]
        if not shares:
            return shares
        drift = float_round(
            charge_amount - sum(shares), precision_rounding=rounding,
        )
        if float_compare(drift, 0.0, precision_rounding=rounding) != 0:
            biggest = max(
                range(len(shares)),
                key=lambda index: moves[index].amount_residual,
            )
            shares[biggest] = float_round(
                shares[biggest] + drift, precision_rounding=rounding,
            )
        return shares

    # ------------------------------------------------------------------
    # Per-partner processing
    # ------------------------------------------------------------------

    def _process_partner(
        self, partner, moves, company, today,
        pct, min_balance, dry_run,
    ):
        """Process a single commercial partner's past-due invoices.

        Returns a dict with:
          - ``charged``: bool (True if a charge was created/logged)
          - ``charge_amount``: float (0 if skipped)
          - ``invoices_charged`` / ``skipped_locked`` / ``skipped_partial``
            / ``skipped_hashed`` / ``skipped_other``: int,
            'invoice_line' mode stats; always 0 for 'separate_invoice'
            mode).
          - ``reason``: str, the PARTNER-level outcome, one of
            ``lw_cc.service.charge.run.line``'s reason values.
          - ``lines``: list of plain dicts, the run-record detail rows
            for this partner. ``_run_for_company`` stamps ``partner_id``
            on each and creates the records after the loop; nothing here
            touches the database, precisely so a savepoint rollback
            cannot erase the audit trail.

        The signature is unchanged and the two pre-existing keys keep
        their exact meaning, so callers and tests that ignore the new
        keys are unaffected.

        Suppression checks (in order), shared by BOTH modes:
          1. Idempotency + cross-mode transition guard: partner already
             has a separate SC invoice this month (checked here,
             unconditionally, BEFORE the mode branch below) -- if so, no
             charge fires at all this month, in either mode. This is
             what stops a same-month mode flip TOWARD invoice_line from
             double-charging a partner via both mechanisms.
          2. Per-customer opt-out: skip if the commercial partner
             has ``lw_cc_service_charge_optout=True``.
          3. min_balance: a PARTNER-level gate in both modes (NOT
             per-invoice) -- total past-due residual across all of the
             partner's eligible invoices must clear the threshold.

        After these, branches on ``company.lw_cc_sc_mode``:
        'invoice_line' delegates to ``_process_partner_invoice_line``.
        'separate_invoice' (default) keeps today's behavior, PLUS one
        additional symmetric guard (the mirror of check 1, in the
        other direction): skip if invoice_line mode already assessed
        interest on any of this partner's eligible invoices this
        month, stopping a same-month mode flip BACK toward
        separate_invoice -- the designed rollback action if
        invoice_line misbehaves -- from double-charging.
        """
        # Idempotency guard: skip if a service charge invoice already
        # exists for this partner in the current month. Prevents
        # duplicate charges from double-fires, retries, or manual runs.
        # Doubles as the invoice_line-mode cross-mode transition guard
        # (see docstring point 1) since it is unconditional here, before
        # the mode branch.
        if self._partner_already_charged_this_month(partner, company, today):
            _logger.info(
                "lw_cc_surcharge: skipping %s "
                "(already charged this month).",
                partner.display_name,
            )
            return {
                'charged': False,
                'charge_amount': 0.0,
                'reason': 'already_charged_month',
                'lines': [self._suppression_line(
                    moves, 'already_charged_month',
                    "A separate SC invoice for %s already exists for "
                    "this partner." % today.strftime("%Y-%m"),
                )],
            }

        # Per-customer opt-out gate (). The service charge is
        # opt-out via the "Exempt from Service Charge" checkbox on the
        # commercial partner (default off): every customer with a
        # past-due balance is charged unless deliberately exempted.
        # Dunning flags are communications decisions and no longer gate
        # the charge; the invoice-level dunning_hold exclusion (disputed
        # invoices) remains in the eligibility domain.
        if partner.lw_cc_service_charge_optout:
            _logger.info(
                "lw_cc_surcharge: skipping %s (service charge "
                "opt-out ticked).",
                partner.display_name,
            )
            return {
                'charged': False,
                'charge_amount': 0.0,
                'reason': 'optout',
                'lines': [self._suppression_line(
                    moves, 'optout',
                    "'Exempt from Service Charge' is ticked on the "
                    "commercial partner.",
                )],
            }

        # Sum past-due residuals.
        total_residual = sum(moves.mapped('amount_residual'))

        # Minimum balance threshold.
        if float_compare(
            total_residual, min_balance,
            precision_rounding=company.currency_id.rounding,
        ) < 0:
            _logger.info(
                "lw_cc_surcharge: skipping %s "
                "(total past-due %.2f < min %.2f).",
                partner.display_name, total_residual, min_balance,
            )
            return {
                'charged': False,
                'charge_amount': 0.0,
                'reason': 'below_min_balance',
                'lines': [self._suppression_line(
                    moves, 'below_min_balance',
                    "Total past-due %.2f is below the %.2f minimum."
                    % (total_residual, min_balance),
                )],
            }

        # Mode branch. 'invoice_line' assesses
        # interest per-invoice instead of one lump-sum charge invoice;
        # everything above this point (idempotency, cross-mode guard,
        # opt-out, min_balance) is shared by both modes.
        if company.lw_cc_sc_mode == 'invoice_line':
            return self._process_partner_invoice_line(
                partner, moves, company, today, pct, dry_run,
            )

        # --- 'separate_invoice' mode (default): unchanged behavior,
        # PLUS the symmetric cross-mode guard immediately below (a
        # defect fix, not part of the original unchanged behavior).

        # Symmetric cross-mode guard: skip if 'invoice_line' mode
        # already assessed interest on ANY of this partner's POSTED
        # out_invoices this month -- across the whole partner, NOT
        # limited to `moves` (the eligibility-domain-filtered
        # recordset for THIS run). This is the mirror image of the
        # guard in _partner_already_charged_this_month (which blocks
        # invoice_line mode when a separate SC invoice already exists
        # this month) -- without it, an operator flipping
        # lw_cc_sc_mode from 'invoice_line' back to 'separate_invoice'
        # mid-month (the designed rollback/failure-recovery action if
        # invoice_line misbehaves in production) would double-charge
        # every partner already assessed by lines that month: a fresh
        # lump-sum charge on top of a residual that already includes
        # this month's line-added interest.
        #
        # Spec-error fix (adverse review -- my own spec error, not a coding
        # mistake): the FIRST version of this guard filtered `moves`,
        # which is scoped to the CURRENT eligibility domain
        # (amount_residual > 0, not dunning_hold, etc.). That is wrong
        # for this specific check: an invoice assessed by lines earlier
        # this month and then paid in full, or that later picks up
        # dunning_hold, drops OUT of `moves` on this run -- but its
        # lw_cc_sc_last_assessed_month marker is still set, and the
        # partner must still be skipped. Searching account.move
        # directly (posted out_invoice, this partner, this company,
        # marker == this month) is immune to that: eligibility for
        # THIS run is irrelevant to whether the marker was stamped
        # EARLIER this month.
        #
        # Reads lw_cc_sc_last_assessed_month directly -- the
        # authoritative per-invoice marker -- rather than the ref
        # matching _partner_already_charged_this_month relies on,
        # which is immune to a genuine SC invoice's dating (the
        # interest line always lands on the invoice's ORIGINAL,
        # possibly long-past, period, so a "dated this month" filter
        # cannot see it). A partner never assessed by lines has no
        # marker on any invoice, so this is a no-op for today's
        # shipping (separate_invoice-only) path.
        month_key = today.strftime("%Y-%m")
        already_assessed_this_month = self.env['account.move'].sudo().search([
            ('partner_id', 'child_of', partner.id),
            ('company_id', '=', company.id),
            ('move_type', '=', 'out_invoice'),
            ('state', '=', 'posted'),
            ('lw_cc_sc_last_assessed_month', '=', month_key),
        ])
        if already_assessed_this_month:
            _logger.info(
                "lw_cc_surcharge: skipping %s (already assessed "
                "this month in invoice_line mode -- invoice(s) %s "
                "carry lw_cc_sc_last_assessed_month=%s; skipping the "
                "separate-invoice charge to avoid double-charging).",
                partner.display_name,
                ", ".join(already_assessed_this_month.mapped('name')),
                month_key,
            )
            return {
                'charged': False,
                'charge_amount': 0.0,
                'reason': 'cross_mode_assessed',
                'lines': [self._suppression_line(
                    moves, 'cross_mode_assessed',
                    "Invoice(s) %s already carry interest assessed in "
                    "%s under invoice-line mode." % (
                        ", ".join(
                            already_assessed_this_month.mapped('name')
                        )[:300],
                        month_key,
                    ),
                )],
            }

        # Calculate charge.
        charge_amount = float_round(
            total_residual * (pct / 100.0),
            precision_rounding=company.currency_id.rounding,
        )

        # Skip zero charges.
        if float_compare(
            charge_amount, 0.0,
            precision_rounding=company.currency_id.rounding,
        ) <= 0:
            _logger.info(
                "lw_cc_surcharge: skipping %s "
                "(computed charge %.2f <= 0).",
                partner.display_name, charge_amount,
            )
            return {
                'charged': False,
                'charge_amount': 0.0,
                'reason': 'zero_charge',
                'lines': [self._suppression_line(
                    moves, 'zero_charge',
                    "%.2f%% of %.2f rounds to %.2f." % (
                        pct, total_residual, charge_amount,
                    ),
                )],
            }

        # Per-invoice attribution of the lump sum, for the report. This
        # is the answer to "which invoices have the most interest
        # accrued" in the mode that actually ships -- a single
        # partner-level row would not be an answer at all.
        shares = self._split_charge_across_moves(
            moves, charge_amount, company, pct,
        )
        charge_lines = [{
            'move_id': move.id,
            'amount_residual': move.amount_residual,
            'computed_interest': share,
            'action': 'would_charge' if dry_run else 'charged',
            'reason': 'ok',
            'reason_detail': '',
        } for move, share in zip(moves, shares)]

        if dry_run:
            # Adverse review: dry-run used to post a chatter note
            # (mail.message + mail.followers rows) on every source
            # invoice -- a write, despite the module's own "dry-run
            # performs zero writes" claim, and on a large book
            # potentially thousands of customer-visible chatter notes
            # from a single readiness check. Log only; a dry run must
            # not mutate a customer-visible record at all.
            _logger.info(
                "lw_cc_surcharge [DRY-RUN]: would charge %s "
                "%.2f (%.2f%% of %.2f past-due across %d invoices: "
                "%s).",
                partner.display_name, charge_amount, pct,
                total_residual, len(moves),
                ", ".join(moves.mapped('name')),
            )
            return {
                'charged': True,
                'charge_amount': charge_amount,
                'reason': 'ok',
                'lines': charge_lines,
            }

        # Live mode: create and post the charge invoice.
        charge_invoice = self.env['account.move'].sudo()._create_service_charge_invoice(
            partner, charge_amount, company, moves,
        )

        if charge_invoice:
            return {
                'charged': True,
                'charge_amount': charge_amount,
                'reason': 'ok',
                'lines': charge_lines,
            }

        # The creator returned an empty recordset: no service charge
        # product resolvable, or the invoice could not be built. It logs
        # the specific cause itself; the report must not silently drop
        # the partner, which would read as "not eligible this month".
        return {
            'charged': False,
            'charge_amount': 0.0,
            'reason': 'unexpected_error',
            'lines': [self._suppression_line(
                moves, 'unexpected_error',
                "Charge invoice creation returned no record (see the "
                "server log for the cause; usually no service charge "
                "product is resolvable).",
            )],
        }

    # ------------------------------------------------------------------
    # Per-partner processing: 'invoice_line' mode
    # ------------------------------------------------------------------

    def _process_partner_invoice_line(
        self, partner, moves, company, today, pct, dry_run,
    ):
        """Assess Charge Terms Interest as a line on each qualifying
        past-due invoice individually, instead of one lump-sum charge
        invoice.

        Each invoice is processed inside its OWN nested savepoint (nested
        inside the per-partner savepoint in ``_run_for_company``) so one
        locked, hash-locked, or partially-paid invoice cannot roll back
        the interest lines already committed on the partner's other
        invoices this tick.

        Per-invoice idempotency: an invoice whose
        ``lw_cc_sc_last_assessed_month`` already equals this month's key
        is skipped outright (no re-read of amounts, no chatter, no
        write) -- it was already assessed this month.

        Base amount depends on ``company.lw_cc_sc_compounding``:
          - 'compound': the invoice's full ``amount_residual`` (prior
            months' interest lines are already folded into it).
          - 'simple': ``amount_residual`` minus the total of this
            invoice's existing interest lines (identified via the
            authoritative ``lw_cc_sc_interest_line`` marker) AND any
            CC processing fee lines (``lw_cc_fee_line``, per
            adverse review) -- so 'simple' mode, which exists
            precisely so interest does NOT compound, never treats a
            card fee as principal and charges interest on it either.
            ``lw_cc_fee_line`` is declared by the CC-fee extension class
            in this module's account_move.py -- read via
            ``getattr(..., False)`` so a build without that class
            keeps working (same defensive pattern as the commission
            report's exclusion). ``lw_cc_sc_interest_
            line`` is NOT getattr-guarded, deliberately inconsistent
            with that: it is declared in THIS module (see
            ``AccountMoveLineInterest``), so it is always present
            wherever this code runs; direct access is correct there,
            and getattr-guarding an own-module field would only hide a
            real bug (e.g. a rename) behind a silent False instead of
            an immediate AttributeError.

        dry_run performs ZERO writes and ZERO line inserts, genuinely:
        the dry-run branch below only ever calls ``_logger.info`` (a
        log line, not any kind of database write -- (adverse
        review: it used to also call ``message_post``, which DOES
        write ``mail.message``/``mail.followers`` rows despite the
        "zero writes" claim, and at production volume that is
        potentially thousands of customer-visible chatter notes from a
        single readiness check) and returns before the try/except
        block that contains the only two write operations in this
        method (``_lw_cc_add_interest_line`` and the
        ``invoice.write(...)`` idempotency/audit stamp).

        Guard categorization (adverse review): hash-lock, fiscal
        lock date, and partial-without-allow are PRE-CHECKED, in the
        same order ``_lw_cc_add_charge_line`` checks them, BEFORE ever
        attempting the write -- not inferred after the fact by
        re-inspecting the invoice post-failure. The prior version
        caught a single generic ``UserError`` from the write attempt
        and guessed the cause from the invoice's post-rollback state,
        which meant every OTHER failure mode (no product configured,
        no income account configured, an unexpected reconciliation
        error) fell into ``skipped_locked`` too -- a staging dry run
        reporting ``skipped_locked=40`` looked like "40 invoices in a
        closed period, expected" when some or all of them were
        actually failing on a missing company config field, and the
        pre-flip readiness gate would pass on that false reading.
        Pre-checking means the write-attempt's ``except`` block now
        only ever catches a genuinely UNEXPECTED failure, counted
        separately as ``skipped_other`` with the real exception
        message logged -- not guessed at.

        Returns a dict shaped like ``_process_partner``'s, plus the
        The ``invoices_charged``, ``skipped_locked``,
        ``skipped_partial``, ``skipped_hashed``, ``skipped_other``.

        Run-record enrichment: every branch below contributes one
        run line for the invoice it handled, so the report is genuinely
        per-invoice in this mode. The partner-level ``reason`` is always
        ``'ok'`` here: by the time this method is reached the partner
        has cleared every PARTNER-level gate (idempotency, cross-mode,
        opt-out, min_balance -- all checked in ``_process_partner``), so
        anything suppressed from here on is suppressed per invoice and
        the reason belongs on that invoice's line, not on the partner.
        """
        month_key = today.strftime("%Y-%m")
        allow_partial = company.lw_cc_sc_partial_policy == 'rereconcile'

        lines = []
        invoices_charged = 0
        skipped_locked = 0
        skipped_partial = 0
        skipped_hashed = 0
        skipped_other = 0
        charge_total = 0.0

        for invoice in moves:
            # --- Per-invoice idempotency (authoritative) -----------------
            if invoice.lw_cc_sc_last_assessed_month == month_key:
                _logger.info(
                    "lw_cc_surcharge [invoice_line]: skipping %s "
                    "(already assessed this month).",
                    invoice.display_name,
                )
                lines.append({
                    'move_id': invoice.id,
                    'amount_residual': invoice.amount_residual,
                    'computed_interest': 0.0,
                    'action': 'skipped',
                    'reason': 'already_charged_month',
                    'reason_detail': (
                        "Interest for %s was already assessed on this "
                        "invoice." % month_key
                    ),
                })
                continue

            # Captured BEFORE any write: on the charged path the
            # interest line lands on this very invoice, so reading
            # amount_residual afterwards would report the post-charge
            # figure and the report's residual column would already
            # include the interest it is meant to explain.
            residual_at_scan = invoice.amount_residual

            # --- Base amount per compounding mode -------------------------
            if company.lw_cc_sc_compounding == 'simple':
                # Subtracts BOTH prior interest lines (this module) and
                # CC fee lines (the CC-fee extension, getattr-guarded -- see the
                # docstring above for why the two reads are
                # deliberately not symmetric).
                non_principal_lines = invoice.line_ids.filtered(
                    lambda l: l.lw_cc_sc_interest_line
                    or getattr(l, 'lw_cc_fee_line', False)
                )
                prior_interest_total = sum(
                    non_principal_lines.mapped('price_subtotal')
                )
                base = max(
                    invoice.amount_residual - prior_interest_total, 0.0,
                )
            else:  # 'compound' (default)
                base = invoice.amount_residual

            amount = float_round(
                base * (pct / 100.0),
                precision_rounding=company.currency_id.rounding,
            )
            if float_compare(
                amount, 0.0,
                precision_rounding=company.currency_id.rounding,
            ) <= 0:
                _logger.info(
                    "lw_cc_surcharge [invoice_line]: skipping %s "
                    "(computed interest %.2f <= 0, base %.2f).",
                    invoice.display_name, amount, base,
                )
                lines.append({
                    'move_id': invoice.id,
                    'amount_residual': invoice.amount_residual,
                    'computed_interest': 0.0,
                    'action': 'skipped',
                    'reason': 'zero_charge',
                    'reason_detail': "%.2f%% of a %.2f base rounds to "
                                     "%.2f." % (pct, base, amount),
                })
                continue

            # --- dry_run: LOG only, ZERO writes, then next invoice ------
            # Adverse review: this used to message_post a chatter
            # note per invoice -- a real write (mail.message +
            # mail.followers), and on production this branch runs once
            # per ELIGIBLE invoice, potentially thousands per tick, on
            # exactly the run the PRE-FLIP-CHECKLIST tells the operator
            # to use as a readiness gate. A dry run must not mutate a
            # customer-visible record; log instead.
            if dry_run:
                _logger.info(
                    "lw_cc_surcharge [DRY-RUN, invoice_line]: would "
                    "assess %s: Charge Terms Interest of %.2f "
                    "(%.2f%% of %.2f base, %s compounding).",
                    invoice.display_name, amount, pct, base,
                    company.lw_cc_sc_compounding,
                )
                lines.append({
                    'move_id': invoice.id,
                    'amount_residual': invoice.amount_residual,
                    'computed_interest': amount,
                    'action': 'would_charge',
                    'reason': 'ok',
                    'reason_detail': "%s compounding, base %.2f." % (
                        company.lw_cc_sc_compounding, base,
                    ),
                })
                invoices_charged += 1
                charge_total += amount
                continue

            # --- Live mode: PRE-CHECK guards, in the same order
            # _lw_cc_add_charge_line checks them, so the bucket is
            # known BEFORE attempting the write (not guessed after a
            # generic failure) ---------------------------------------
            if invoice.journal_id.restrict_mode_hash_table:
                skipped_hashed += 1
                _logger.info(
                    "lw_cc_surcharge [invoice_line]: skipping %s "
                    "(hash-locked journal %s).",
                    invoice.display_name, invoice.journal_id.display_name,
                )
                lines.append({
                    'move_id': invoice.id,
                    'amount_residual': invoice.amount_residual,
                    'computed_interest': 0.0,
                    'action': 'skipped',
                    'reason': 'hash_locked',
                    'reason_detail': (
                        "Journal %s is hash-locked; %.2f not charged."
                        % (invoice.journal_id.display_name, amount)
                    ),
                })
                continue
            try:
                # The exact core method _lw_cc_add_charge_line relies
                # on -- called here directly, not reimplemented, so
                # this can never disagree with the real guard.
                invoice._check_fiscal_lock_dates()
            except UserError as exc:
                skipped_locked += 1
                _logger.info(
                    "lw_cc_surcharge [invoice_line]: skipping %s "
                    "(fiscal lock date).", invoice.display_name,
                )
                lines.append({
                    'move_id': invoice.id,
                    'amount_residual': invoice.amount_residual,
                    'computed_interest': 0.0,
                    'action': 'skipped',
                    'reason': 'fiscal_locked',
                    'reason_detail': (
                        "Fiscal lock date blocks this period; %.2f not "
                        "charged. %s" % (amount, exc)
                    )[:512],
                })
                continue
            if not allow_partial and invoice.payment_state == 'partial':
                skipped_partial += 1
                _logger.info(
                    "lw_cc_surcharge [invoice_line]: skipping %s "
                    "(partially paid; lw_cc_sc_partial_policy="
                    "'skip_partial').", invoice.display_name,
                )
                lines.append({
                    'move_id': invoice.id,
                    'amount_residual': invoice.amount_residual,
                    'computed_interest': 0.0,
                    'action': 'skipped',
                    'reason': 'partial_skipped',
                    'reason_detail': (
                        "Partially paid and the partial-payment policy "
                        "is 'skip_partial'; %.2f not charged." % amount
                    ),
                })
                continue

            # --- Live mode: add the line, in its own nested savepoint ---
            try:
                with self.env.cr.savepoint():
                    invoice._lw_cc_add_interest_line(
                        amount, company, allow_partial,
                    )
                    # skip_is_manually_modified=True (adverse
                    # review): without it, core's write() force-sets
                    # is_manually_modified=True on this idempotency/
                    # audit stamp too, flagging an automated cron
                    # write as a hand edit -- verified in the real v19
                    # source (account_move.py write()); see the same
                    # fix and rationale in _lw_cc_add_charge_line.
                    invoice.with_context(
                        skip_is_manually_modified=True,
                    ).write({
                        'lw_cc_sc_last_assessed_month': month_key,
                        'lw_cc_sc_total_assessed': (
                            invoice.lw_cc_sc_total_assessed + amount
                        ),
                    })
            except UserError as exc:
                # Everything reaching here is UNEXPECTED -- hash lock,
                # fiscal lock date, and partial-without-allow were all
                # already ruled out above. Counted and logged
                # separately, with the real message, rather than
                # folded into skipped_locked (adverse review).
                skipped_other += 1
                _logger.warning(
                    "lw_cc_surcharge [invoice_line]: skipping %s "
                    "(UNEXPECTED failure, not locked/hashed/partial -- "
                    "%s).", invoice.display_name, exc,
                )
                lines.append({
                    'move_id': invoice.id,
                    'amount_residual': residual_at_scan,
                    'computed_interest': 0.0,
                    'action': 'skipped',
                    'reason': 'unexpected_error',
                    'reason_detail': (str(exc) or exc.__class__.__name__)[:512],
                })
                continue

            lines.append({
                'move_id': invoice.id,
                'amount_residual': residual_at_scan,
                'computed_interest': amount,
                'action': 'charged',
                'reason': 'ok',
                'reason_detail': "%s compounding, base %.2f." % (
                    company.lw_cc_sc_compounding, base,
                ),
            })
            invoices_charged += 1
            charge_total += amount

        _logger.info(
            "lw_cc_surcharge [invoice_line]: partner %s [%s]: "
            "charged %d invoice(s) totaling %.2f; skipped_locked=%d "
            "skipped_partial=%d skipped_hashed=%d skipped_other=%d.",
            partner.display_name,
            'DRY-RUN' if dry_run else 'LIVE',
            invoices_charged, charge_total,
            skipped_locked, skipped_partial, skipped_hashed, skipped_other,
        )

        return {
            'charged': invoices_charged > 0,
            'charge_amount': charge_total,
            'invoices_charged': invoices_charged,
            'skipped_locked': skipped_locked,
            'skipped_partial': skipped_partial,
            'skipped_hashed': skipped_hashed,
            'skipped_other': skipped_other,
            # See the docstring: the partner itself cleared every
            # partner-level gate, so suppression here is per invoice and
            # lives on the lines.
            'reason': 'ok',
            'lines': lines,
        }

    # ------------------------------------------------------------------
    # Idempotency helper
    # ------------------------------------------------------------------

    def _partner_already_charged_this_month(self, partner, company, today):
        """Whether the partner already has a SEPARATE service charge
        invoice this month.

        A SINGLE check: ref-prefix match. A posted out_invoice dated
        this month carrying the ``SC/<YYYY-MM>/`` ref prefix, stamped
        UNCONDITIONALLY by ``_create_service_charge_invoice`` on every
        invoice it creates. Proven necessary and sufficient:
          - ``'ref'`` is set in that function's ``invoice_vals`` dict
            with no conditional branch that skips it, and every early
            ``return`` in that function happens BEFORE ``invoice_vals``
            is even built -- so if a charge invoice was created at
            all, it carries this ref.
          - ``account.move.ref`` is a plain, non-computed ``Char``
            field in core (nothing recomputes or clears it later).
          - grepping ``lw_cc_surcharge`` for every write to
            ``.ref``/``'ref':`` finds exactly two: this stamp, and the
            unrelated ``CCS/<tx reference>`` fallback invoice from
            ``_create_cc_surcharge_invoice`` -- a DIFFERENT charge (the
            credit-card processing fee, not Charge Terms Interest) that
            was never supposed to gate this check either.

        REMOVED (adverse review -- worst finding of the build):
        a second "product match" check used to also flag any posted
        invoice dated this month carrying a line with the SC product,
        on the theory that a genuine SC invoice's line always carries
        that product, filtered to exclude Charge Terms Interest lines
        (``lw_cc_sc_interest_line``) so an invoice_line-mode assessment
        wouldn't trip its own guard. That filter fixed ONE member of a
        false-positive class and left another: the
        same-invoice CC fee line (models/account_move.py) resolves
        the IDENTICAL SC
        product and never sets ``lw_cc_sc_interest_line`` -- so it
        matched "product present, not an interest line" exactly like a
        genuine SC invoice did. Effect: a partner who paid ANY in-month
        invoice by card got their ENTIRE month's Charge Terms Interest
        silently suppressed under ``invoice_line`` mode, logged as
        "already charged this month" so it looked deliberate rather
        than a bug. The CCS fallback invoice (dated ``context_today``
        by ``_create_cc_surcharge_invoice``, so ALWAYS in-month) made
        this worse: any customer whose fee took that fallback path was
        guaranteed to escape that month's interest. The ref-only check
        above has no equivalent false-positive surface: a CC fee line
        is not even an invoice (it's a line added to an EXISTING
        invoice) and never carries an ``SC/<month>/`` ref.

        This prevents duplicate charges from double-fires, retries, or
        manual cron triggers within the same billing period, and is
        the cross-mode transition guard direction described in
        ``_process_partner``'s docstring (point 1).

        :returns: True if a genuine separate charge invoice exists
            this month.
        """
        month_start = today.replace(day=1)
        month = today.strftime("%Y-%m")
        ref_match = self.env['account.move'].sudo().search([
            ('partner_id', 'child_of', partner.id),
            ('company_id', '=', company.id),
            ('move_type', '=', 'out_invoice'),
            ('state', '=', 'posted'),
            ('invoice_date', '>=', month_start),
            ('invoice_date', '<=', today),
            ('ref', '=like', 'SC/%s/%%' % month),
        ], limit=1)
        return bool(ref_match)
