# -*- coding: utf-8 -*-
"""Post-init hooks for lw_cc_surcharge.

Odoo 19 hook signature is ``(env)``. The single manifest hook
``post_init_hook`` establishes the module's shipping posture on a
FRESH INSTALL and logs it for the deployment audit trail. This is a
fresh store module, so there are no upgrade migrations: install-time
seeding lives here, and only here.

Posture since 19.0.5.0.0 (carried over from the source builds):
DRY-RUN-ARMED, not INERT.

  * cron ACTIVE, nextcall = the first of the month AFTER install,
    06:00 UTC (computed here, not the XML literal)
  * ``lw_cc_surcharge_enabled = True``
  * ``lw_cc_surcharge_dry_run = True``
  * ``lw_cc_surcharge_backend_wizard = True`` (staff Pay-wizard
    surcharge checkbox ships live: it changes nothing by itself, the
    wizard gate also requires the surcharge to be enabled with a
    positive percentage)
  * ``lw_cc_surcharge_portal_uplift = False`` (ships INERT)

The cron running is not the same as money moving. In dry-run the runner
creates zero ``account.move`` / ``account.move.line`` rows and posts no
chatter; its only write is the ``lw_cc.service.charge.run`` audit
report. The charges need an operator to set a Service Charge Income
Account and untick dry-run, and
``res_company._check_live_mode_requires_income_account`` blocks the
untick without the account.

The hook also seeds ``lw_cc_surcharge_product_id`` and the
invoice-line mode switches on companies that already exist at install time (field
defaults only reach companies created afterwards).
"""
import logging
from datetime import datetime

from odoo import fields

_logger = logging.getLogger(__name__)

CRON_XMLID = 'lw_cc_surcharge.ir_cron_monthly_service_charge'
PRODUCT_XMLID = 'lw_cc_surcharge.product_service_charge'

# UTC hour of the monthly tick. Early enough to be finished long before
# a business day begins, late enough that a failure leaves someone a
# morning to notice it.
CRON_HOUR_UTC = 6

# The DRY-RUN-ARMED company posture. Both keys are written in ONE write
# so _check_live_mode_requires_income_account sees the final state:
# enabled=True on its own, against a company that already had dry_run
# off and no income account, would trip the constraint.
ARMED_COMPANY_POSTURE = {
    'lw_cc_surcharge_enabled': True,
    'lw_cc_surcharge_dry_run': True,
}


def first_of_next_month_utc(today=None):
    """First day of the month AFTER ``today``, at ``CRON_HOUR_UTC``.

    Computed rather than hardcoded because the first tick must be the
    first of the month after the module lands, whatever day that is. A
    literal first-tick date goes stale the moment the install slips past
    it: install in October against a hardcoded September date and the
    nextcall is already in the past, so the scheduler runs the engine
    on its very next pass instead of on the 1st. Harmless to the books
    in dry-run, but an operator doing a careful off-hours install
    should not get a surprise engine run -- and if dry-run has been
    unticked by then, "fires immediately on install" is a genuinely bad
    surprise.

    Plain date arithmetic rather than ``dateutil.relativedelta``: this
    module imports ``odoo`` and nothing else, and rolling a month
    forward is one line either way.
    """
    today = today or fields.Date.today()
    year, month = today.year, today.month
    if month == 12:
        year, month = year + 1, 1
    else:
        month += 1
    return datetime(year, month, 1, CRON_HOUR_UTC, 0, 0)

SC_MODE_DEFAULTS = {
    'lw_cc_sc_mode': 'separate_invoice',
    'lw_cc_sc_compounding': 'compound',
    'lw_cc_sc_partial_policy': 'rereconcile',
}


def post_init_hook(env):
    """Assert the DRY-RUN-ARMED posture at install, and log it.

    The cron XML already ships ``active=True`` with the right nextcall,
    but it is ``noupdate="1"`` and a reinstall over an old
    ir_model_data row can leave previous values in place, so this
    re-asserts rather than assuming. Idempotent by construction: it
    writes only what is not already correct.
    """
    try:
        cron = env.ref(CRON_XMLID, raise_if_not_found=False)
        if cron:
            target_nextcall = first_of_next_month_utc()
            cron_vals = {}
            if not cron.active:
                cron_vals['active'] = True
            if not cron.nextcall or cron.nextcall > target_nextcall:
                cron_vals['nextcall'] = target_nextcall
            if cron_vals:
                cron.sudo().write(cron_vals)
                _logger.info(
                    "lw_cc_surcharge: armed the monthly service "
                    "charge cron at install (%s).", cron_vals,
                )
            _logger.info(
                "lw_cc_surcharge: post_init_hook complete. "
                "Cron active=%s, nextcall=%s. Module is DRY-RUN-ARMED: "
                "the cron reports monthly and creates no charge "
                "invoices while dry-run is on.",
                cron.active, cron.nextcall,
            )
        else:
            _logger.error(
                "lw_cc_surcharge: cron %s not found at install.",
                CRON_XMLID,
            )
    except Exception:  # noqa: BLE001
        _logger.exception(
            "lw_cc_surcharge: post_init_hook error; "
            "module installed but cron state unverified."
        )

    # Assert the company-side half of the DRY-RUN-ARMED posture. The
    # field defaults already give this to companies created after the
    # install, and v19's _init_column fills the default into existing
    # company rows when it creates the columns -- but a REINSTALL over
    # columns that already exist gets neither, so write it explicitly.
    #
    # Deliberately unconditional on enabled/dry_run rather than
    # "only if unset": a Boolean cannot distinguish "unset" from
    # "deliberately False" after _init_column has run, and this hook
    # only ever fires on a fresh install, where there is no operator
    # decision to preserve. dry_run is forced True, which is the safe
    # direction in every case.
    try:
        companies = env['res.company'].sudo().search([])
        stale = companies.filtered(
            lambda c: not c.lw_cc_surcharge_enabled
            or not c.lw_cc_surcharge_dry_run
        )
        if stale:
            stale.write(dict(ARMED_COMPANY_POSTURE))
            _logger.info(
                "lw_cc_surcharge: set the DRY-RUN-ARMED posture "
                "(enabled=True, dry_run=True) on %d compan(y/ies) at "
                "install.", len(stale),
            )
    except Exception:  # noqa: BLE001
        _logger.exception(
            "lw_cc_surcharge: post_init_hook error while setting the "
            "DRY-RUN-ARMED company posture; module installed but the "
            "company flags could not be set."
        )

    # Seed lw_cc_surcharge_product_id on companies where it is unset.
    # The field default covers companies created AFTER install; this
    # covers companies that already exist at install time.
    try:
        product = env.ref(PRODUCT_XMLID, raise_if_not_found=False)
        if product:
            companies = env['res.company'].sudo().search([
                ('lw_cc_surcharge_product_id', '=', False),
            ])
            if companies:
                companies.sudo().write({
                    'lw_cc_surcharge_product_id': product.id,
                })
                _logger.info(
                    "lw_cc_surcharge: seeded service charge product "
                    "on %d compan(y/ies) at install.",
                    len(companies),
                )
    except Exception:  # noqa: BLE001
        _logger.exception(
            "lw_cc_surcharge: post_init_hook error while seeding "
            "the service charge product; module installed but the "
            "company default could not be set."
        )

    # Seed the service-charge mode switches on companies that already
    # exist at install time (the field defaults only cover companies
    # created AFTER install). This is not cosmetic: every consumer
    # compares against the string it wants, so an unset value silently
    # selects the OTHER branch -- an unset lw_cc_sc_partial_policy reads
    # as skip_partial rather than the configured rereconcile.
    try:
        companies = env['res.company'].sudo()
        for field_name, value in SC_MODE_DEFAULTS.items():
            if field_name not in companies._fields:
                continue
            unset = companies.search([(field_name, '=', False)])
            if unset:
                unset.sudo().write({field_name: value})
                _logger.info(
                    "lw_cc_surcharge: seeded %s=%s on %d "
                    "compan(y/ies) at install.",
                    field_name, value, len(unset),
                )
    except Exception:  # noqa: BLE001
        _logger.exception(
            "lw_cc_surcharge: post_init_hook error while seeding the "
            "service charge mode switches; module installed but the "
            "company defaults could not be set."
        )

    # Arm the backend Pay-wizard surcharge (ships live). A stored-field
    # default only covers records created after the column exists; a
    # fresh install on a company that already exists leaves the
    # pre-existing row NULL/False. Arming this one switch changes
    # nothing by itself: the wizard's own gate also requires
    # lw_cc_surcharge_enabled and a positive cc_pct.
    _post_init_arm_backend_wizard(env)


def _post_init_arm_backend_wizard(env):
    """Arm the backend Pay-wizard surcharge checkbox on existing companies."""
    companies = env['res.company'].search([
        ('lw_cc_surcharge_backend_wizard', '=', False),
    ])
    if companies:
        companies.write({'lw_cc_surcharge_backend_wizard': True})
        _logger.info(
            "lw_cc_surcharge: backend Pay-wizard surcharge "
            "armed on %d companies (ships live).",
            len(companies),
        )
