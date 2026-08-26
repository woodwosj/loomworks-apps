# -*- coding: utf-8 -*-
"""Audit run records for the monthly service charge.

Every tick of ``lw_cc.service.charge.runner`` -- dry-run or live --
writes one ``lw_cc.service.charge.run`` header plus one
``lw_cc.service.charge.run.line`` per eligible invoice (or one
partner-level row when the whole partner was suppressed). This is the
report the accounting team asked for: "which invoices have the most interest
accrued", answerable by sorting the line list on Computed Interest.

Design notes that matter when reading or extending this:

* THESE ARE AUDIT OBJECTS, NOT ACCOUNTING OBJECTS. Creating them in
  dry-run mode is correct and intended. A dry run still creates ZERO
  ``account.move`` / ``account.move.line`` rows and still posts zero
  chatter -- see the runner's dry-run branches.

* SUPPRESSION IS THE DOMINANT REAL-WORLD PATH. A census on a
  production snapshot found many past-due partners, of which only
  a handful would actually be charged; everyone else was suppressed by
  one gate or another. A report that only listed charges would have
  shown almost nothing and explained less. So a suppressed partner
  gets a partner-level row (``move_id`` empty) carrying the reason.

* ``computed_interest`` IS MONEY, NOT INTENT. It is populated only on
  rows whose ``action`` is ``charged`` or ``would_charge``; every
  skipped row carries 0.00 and puts the amount that WOULD have been
  charged in ``reason_detail`` instead. That keeps the invariant
  ``sum(line.computed_interest) == run.total_computed`` true, so the
  list view's column sum can be read directly against the header
  without silently double-counting suppressed money.

Security posture (see ``security/ir.model.access.csv``; CSV files
cannot carry comments, so the justification lives here):

* Accounting managers get READ and UNLINK, but NOT create or write. The
  runner is the only writer, and it writes via ``sudo()``. Denying
  write means a run is either intact or gone -- its contents can never
  be edited after the fact, which is the whole point of an audit
  record. Unlink is granted deliberately: these accumulate one header
  plus N lines every month forever, most of them dry-run noise from
  the pre-go-live period, and accounting needs to be able to prune old runs
  without a developer. Deleting a run destroys no accounting object --
  the charge invoices it describes are separate records and are
  untouched.
* Invoicing users (``account.group_account_invoice``) get READ only,
  matching the pattern the module's other two ACL pairs already use.
"""
from odoo import api, fields, models

# Reason vocabulary, shared by both models. Every value below maps to a
# real branch in ``lw_cc.service.charge.runner`` -- do not add a value
# here without a code path that emits it, and do not emit a string from
# the runner that is not in this list (Odoo raises on an unknown
# Selection value at create time).
RUN_LINE_REASONS = [
    ('ok', "Charged / Would Charge"),
    ('already_charged_month', "Already Charged This Month"),
    ('optout', "Exempt from Service Charge"),
    ('below_min_balance', "Below Minimum Past-Due Balance"),
    ('zero_charge', "Computed Charge Rounds To Zero"),
    ('cross_mode_assessed', "Already Assessed This Month (Invoice-Line Mode)"),
    ('hash_locked', "Hash-Locked Journal"),
    ('fiscal_locked', "Fiscal Lock Date"),
    ('partial_skipped', "Partially Paid (Skip Policy)"),
    ('unexpected_error', "Unexpected Error"),
]

RUN_LINE_ACTIONS = [
    ('charged', "Charged"),
    ('would_charge', "Would Charge"),
    ('skipped', "Skipped"),
]

SC_MODE_SELECTION = [
    ('separate_invoice', "Separate Monthly Invoice"),
    ('invoice_line', "Line on Existing Past-Due Invoice"),
]


class LwCcServiceChargeRun(models.Model):
    _name = 'lw_cc.service.charge.run'
    _description = 'Service Charge Run'
    _order = 'run_date desc, id desc'

    name = fields.Char(
        string="Reference",
        required=True,
        readonly=True,
        index=True,
        help="Run label, e.g. 'SC Run 2026-09-01 [DRY-RUN]'.",
    )
    run_date = fields.Datetime(
        string="Run On",
        required=True,
        readonly=True,
        index=True,
        default=fields.Datetime.now,
        help="When the runner executed.",
    )
    company_id = fields.Many2one(
        'res.company',
        string="Company",
        required=True,
        readonly=True,
        index=True,
        ondelete='cascade',
    )
    currency_id = fields.Many2one(
        related='company_id.currency_id',
        string="Currency",
        readonly=True,
        # Stored so the monetary columns still render on a run whose
        # company was later reconfigured, and so list-view sums work.
        store=True,
    )
    mode = fields.Selection(
        [('dry_run', "Dry Run"), ('live', "Live")],
        string="Mode",
        required=True,
        readonly=True,
        index=True,
        help=(
            "Dry Run computed the charges and wrote this report only. "
            "Live also created and posted the charge invoices / interest "
            "lines."
        ),
    )
    sc_mode = fields.Selection(
        SC_MODE_SELECTION,
        string="Delivery Mode",
        readonly=True,
        help=(
            "Mirror of the company's Charge Terms Interest Mode at the "
            "moment this run executed. Kept on the run because the "
            "company setting can be changed afterwards."
        ),
    )
    pct = fields.Float(
        string="Rate %",
        readonly=True,
        digits=(16, 4),
        help="Monthly service charge percentage applied by this run.",
    )
    partner_count = fields.Integer(
        string="Partners Scanned",
        readonly=True,
    )
    charged_partner_count = fields.Integer(
        string="Partners Charged",
        readonly=True,
    )
    skipped_partner_count = fields.Integer(
        string="Partners Skipped",
        readonly=True,
    )
    total_computed = fields.Monetary(
        string="Total Computed",
        currency_field='currency_id',
        readonly=True,
        help=(
            "Total charged (live) or that would have been charged "
            "(dry run). Equals the sum of the detail lines' Computed "
            "Interest."
        ),
    )
    line_ids = fields.One2many(
        'lw_cc.service.charge.run.line',
        'run_id',
        string="Detail",
        readonly=True,
    )
    line_count = fields.Integer(
        string="Detail Lines",
        compute='_compute_line_count',
    )
    state = fields.Selection(
        [('done', "Completed"), ('error', "Completed With Errors")],
        string="Status",
        default='done',
        required=True,
        readonly=True,
        help=(
            "'Completed With Errors' means at least one partner or "
            "invoice hit an unexpected failure -- look for lines whose "
            "reason is 'Unexpected Error'. The rest of the run still "
            "completed: failures are isolated per partner."
        ),
    )
    note = fields.Text(
        string="Summary",
        readonly=True,
    )

    @api.depends('line_ids')
    def _compute_line_count(self):
        counts = dict(self.env['lw_cc.service.charge.run.line']._read_group(
            [('run_id', 'in', self.ids)],
            groupby=['run_id'],
            aggregates=['__count'],
        ))
        for run in self:
            run.line_count = counts.get(run, 0)


class LwCcServiceChargeRunLine(models.Model):
    _name = 'lw_cc.service.charge.run.line'
    _description = 'Service Charge Run Detail'
    # Default order IS the report: biggest accrued interest first. 
    # opens this to answer "which invoices have the most interest
    # accrued", so that answer must be the first screen, not a sort the
    # reader has to know to apply.
    _order = 'computed_interest desc, id'

    run_id = fields.Many2one(
        'lw_cc.service.charge.run',
        string="Run",
        required=True,
        readonly=True,
        index=True,
        ondelete='cascade',
    )
    company_id = fields.Many2one(
        related='run_id.company_id',
        string="Company",
        store=True,
        index=True,
        readonly=True,
    )
    currency_id = fields.Many2one(
        related='run_id.currency_id',
        string="Currency",
        store=True,
        readonly=True,
    )
    run_date = fields.Datetime(
        related='run_id.run_date',
        string="Run On",
        store=True,
        index=True,
        readonly=True,
    )
    mode = fields.Selection(
        related='run_id.mode',
        string="Mode",
        store=True,
        readonly=True,
    )
    partner_id = fields.Many2one(
        'res.partner',
        string="Customer",
        required=True,
        readonly=True,
        index=True,
        ondelete='cascade',
    )
    move_id = fields.Many2one(
        'account.move',
        string="Invoice",
        readonly=True,
        index=True,
        ondelete='set null',
        help=(
            "The past-due invoice this line describes. Empty on a "
            "partner-level row -- the whole partner was suppressed "
            "before any single invoice was considered."
        ),
    )
    amount_residual = fields.Monetary(
        string="Past-Due Residual",
        currency_field='currency_id',
        readonly=True,
        help=(
            "The invoice's open residual at run time, or the partner's "
            "total past-due residual on a partner-level row."
        ),
    )
    computed_interest = fields.Monetary(
        string="Computed Interest",
        currency_field='currency_id',
        readonly=True,
        help=(
            "Money actually charged (live) or that would be charged "
            "(dry run). Always 0.00 on a skipped line -- a suppressed "
            "amount is reported in Detail instead, so this column's sum "
            "matches the run's Total Computed."
        ),
    )
    action = fields.Selection(
        RUN_LINE_ACTIONS,
        string="Action",
        required=True,
        readonly=True,
        index=True,
    )
    reason = fields.Selection(
        RUN_LINE_REASONS,
        string="Reason",
        required=True,
        readonly=True,
        index=True,
    )
    reason_detail = fields.Char(
        string="Detail",
        readonly=True,
        help=(
            "Free text for the cases a Selection cannot carry: the "
            "exception message on an unexpected failure, or the "
            "suppressed amount on a skipped line."
        ),
    )
