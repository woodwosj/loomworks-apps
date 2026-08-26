# -*- coding: utf-8 -*-
"""Config guard tests: live mode must not be reachable without a
dedicated service-charge income account.

Also covers the Settings form save path (res.config.settings), which
the other tests in this module bypass by writing res.company directly."""
from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install', 'lw_cc_surcharge')
class TestConfigGuards(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.income_account = cls.env['account.account'].search(
            [('account_type', '=', 'income')], limit=1)

    def test_01_live_mode_without_income_account_blocked(self):
        self.company.lw_cc_surcharge_income_account_id = False
        with self.assertRaises(ValidationError):
            self.company.write({
                'lw_cc_surcharge_enabled': True,
                'lw_cc_surcharge_dry_run': False,
            })

    def test_02_live_mode_with_income_account_allowed(self):
        self.company.lw_cc_surcharge_income_account_id = self.income_account
        self.company.write({
            'lw_cc_surcharge_enabled': True,
            'lw_cc_surcharge_dry_run': False,
        })
        self.assertTrue(self.company.lw_cc_surcharge_enabled)

    def test_03_dry_run_without_income_account_allowed(self):
        self.company.lw_cc_surcharge_income_account_id = False
        self.company.write({
            'lw_cc_surcharge_enabled': True,
            'lw_cc_surcharge_dry_run': True,
        })
        self.assertTrue(self.company.lw_cc_surcharge_dry_run)

    def test_04_settings_form_writes_company_fields(self):
        """Saving the Settings form writes through to res.company.

        Every other test in this module writes res.company directly;
        this exercises the res.config.settings save path the Settings
        UI actually uses (related fields with readonly=False plus
        execute()), closing the gap where a broken related field would
        leave the Settings page green while never reaching the company.
        The company is arranged into a known off state first so the
        assertions discriminate even on a database where the module is
        already live (e.g. the staging demo snapshot).
        """
        self.company.lw_cc_surcharge_income_account_id = self.income_account
        self.company.lw_cc_surcharge_enabled = False
        self.company.lw_cc_surcharge_pct = 1.5

        settings = self.env['res.config.settings'].create({})
        settings.lw_cc_surcharge_enabled = True
        settings.lw_cc_surcharge_pct = 2.5
        settings.execute()

        self.assertTrue(self.company.lw_cc_surcharge_enabled)
        self.assertEqual(self.company.lw_cc_surcharge_pct, 2.5)

    # ------------------------------------------------------------------
    # Release review: the BIN reference table decides
    # CREDIT vs DEBIT, i.e. who gets surcharged. Invoicing users may read
    # it, never rewrite it; the CSV loader is system-only.
    # ------------------------------------------------------------------

    def _make_invoicing_user(self):
        return self.env['res.users'].with_context(
            no_reset_password=True, mail_create_nolog=True,
        ).create({
            'name': 'CC Surcharge Test Invoicing User',
            'login': 'lw_cc_surcharge_test_invoicing',
            'email': 'lw_cc_surcharge_test_invoicing@example.com',
            'group_ids': [Command.set((
                self.env.ref('base.group_user')
                + self.env.ref('account.group_account_invoice')
            ).ids)],
        })

    def test_05_invoicing_user_cannot_write_bin_records(self):
        """account.group_account_invoice is read-only on lw_cc.bin.record.

        The shipped ACL row used to grant write (1,1,0,0), which let any
        invoicing user flip a DEBIT range to CREDIT. No code path needs
        it: every reader goes through sudo()."""
        record = self.env['lw_cc.bin.record'].create({
            'bin_start': '999990',
            'bin_end': '999999',
            'card_type': 'DEBIT',
            'network': 'TEST',
        })
        user = self._make_invoicing_user()
        Bin = self.env['lw_cc.bin.record'].with_user(user)
        # read is allowed
        self.assertEqual(Bin.browse(record.id).card_type, 'DEBIT')
        with self.assertRaises(AccessError):
            Bin.browse(record.id).write({'card_type': 'CREDIT'})
        with self.assertRaises(AccessError):
            Bin.create({
                'bin_start': '999980', 'bin_end': '999989',
                'card_type': 'CREDIT', 'network': 'TEST',
            })
        self.assertEqual(record.card_type, 'DEBIT')

    def test_06_load_csv_is_system_only(self):
        """load_csv is an RPC-callable @api.model method that opens a
        server-side path; it must refuse non-system users BEFORE touching
        the filesystem (the path below does not exist, so reaching
        open() would raise FileNotFoundError instead of AccessError)."""
        user = self._make_invoicing_user()
        with self.assertRaises(AccessError):
            self.env['lw_cc.bin.record'].with_user(user).load_csv(
                '/nonexistent/lw_cc-bin-test.csv')
        # The superuser path (odoo-bin shell) still reaches the loader.
        with self.assertRaises(FileNotFoundError):
            self.env['lw_cc.bin.record'].load_csv(
                '/nonexistent/lw_cc-bin-test.csv')
