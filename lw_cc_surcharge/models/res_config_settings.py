# -*- coding: utf-8 -*-
"""res.config.settings exposure for the CC Surcharge module.

All fields proxy to ``res.company`` via ``related='company_id.xxx'``
with ``readonly=False`` so the Settings page is the single configuration
surface. This mirrors the standard related-company-field pattern but
exposes the fields on the standard Odoo Settings page rather than only
on the company form.
"""
from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # ------------------------------------------------------------------
    # Master toggle + safety
    # ------------------------------------------------------------------
    lw_cc_surcharge_enabled = fields.Boolean(
        string="Enable CC Surcharge & Service Charge",
        related='company_id.lw_cc_surcharge_enabled',
        readonly=False,
    )
    lw_cc_surcharge_dry_run = fields.Boolean(
        string="Service Charge Dry-Run",
        related='company_id.lw_cc_surcharge_dry_run',
        readonly=False,
    )

    # ------------------------------------------------------------------
    # Service charge parameters
    # ------------------------------------------------------------------
    lw_cc_surcharge_pct = fields.Float(
        string="Monthly Service Charge %",
        related='company_id.lw_cc_surcharge_pct',
        readonly=False,
    )
    lw_cc_surcharge_min_balance = fields.Monetary(
        string="Minimum Past-Due Balance",
        related='company_id.lw_cc_surcharge_min_balance',
        readonly=False,
    )
    lw_cc_surcharge_past_due_days = fields.Integer(
        string="Past-Due Grace Days",
        related='company_id.lw_cc_surcharge_past_due_days',
        readonly=False,
    )

    # ------------------------------------------------------------------
    # Surcharge parameters
    # ------------------------------------------------------------------
    lw_cc_surcharge_cc_pct = fields.Float(
        string="Credit Card Surcharge %",
        related='company_id.lw_cc_surcharge_cc_pct',
        readonly=False,
    )

    # ------------------------------------------------------------------
    # Product + income account mapping
    # ------------------------------------------------------------------
    lw_cc_surcharge_product_id = fields.Many2one(
        'product.product',
        string="Service Charge Product",
        related='company_id.lw_cc_surcharge_product_id',
        readonly=False,
    )
    lw_cc_surcharge_income_account_id = fields.Many2one(
        'account.account',
        string="Service Charge Income Account",
        related='company_id.lw_cc_surcharge_income_account_id',
        readonly=False,
    )
    lw_cc_surcharge_cc_income_account_id = fields.Many2one(
        'account.account',
        string="CC Surcharge Income Account",
        related='company_id.lw_cc_surcharge_cc_income_account_id',
        readonly=False,
    )
    lw_cc_surcharge_applicable_term_ids = fields.Many2many(
        'account.payment.term',
        string="Applicable Payment Terms",
        related='company_id.lw_cc_surcharge_applicable_term_ids',
        readonly=False,
    )

    # ------------------------------------------------------------------
    # Charge Terms Interest delivery mode
    # ------------------------------------------------------------------
    lw_cc_sc_mode = fields.Selection(
        string="Charge Terms Interest Mode",
        related='company_id.lw_cc_sc_mode',
        readonly=False,
    )
    lw_cc_sc_compounding = fields.Selection(
        string="Charge Terms Interest Compounding",
        related='company_id.lw_cc_sc_compounding',
        readonly=False,
    )
    lw_cc_sc_partial_policy = fields.Selection(
        string="Charge Terms Interest Partial-Payment Policy",
        related='company_id.lw_cc_sc_partial_policy',
        readonly=False,
    )

    lw_cc_bin_record_count = fields.Integer(
        string="BIN Records Loaded",
        compute='_compute_lw_cc_bin_record_count',
    )

    @api.depends('company_id')
    def _compute_lw_cc_bin_record_count(self):
        count = self.env['lw_cc.bin.record'].sudo().search_count([])
        for settings in self:
            settings.lw_cc_bin_record_count = count

    def action_open_bin_records(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'BIN Records',
            'res_model': 'lw_cc.bin.record',
            'view_mode': 'list,form',
            'target': 'current',
        }

# ===================================================================
# Below: merged from the portal-uplift companion module: res_config_settings.py
# ===================================================================
# -*- coding: utf-8 -*-
"""res.config.settings exposure for the portal uplift flag.

Same pattern as lw_cc_surcharge's settings: related company field,
readonly=False, so the Settings page is the single configuration surface.
"""
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    lw_cc_surcharge_portal_uplift = fields.Boolean(
        string="Portal Fee Uplift",
        related='company_id.lw_cc_surcharge_portal_uplift',
        readonly=False,
    )
    lw_cc_surcharge_backend_wizard = fields.Boolean(
        string="Backend Pay-Wizard Surcharge",
        related='company_id.lw_cc_surcharge_backend_wizard',
        readonly=False,
    )
