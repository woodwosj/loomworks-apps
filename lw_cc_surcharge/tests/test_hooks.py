# -*- coding: utf-8 -*-
"""post_init_hook contract tests for lw_cc_surcharge.

The merged module has ONE manifest hook, ``post_init_hook``, which
asserts the DRY-RUN-ARMED shipping posture and also arms the backend
Pay-wizard surcharge checkbox (ships live) and never arms the portal
uplift. Two things are asserted here that nothing else in this suite
covers:

1. RESOLVABILITY. ``odoo/modules/loading.py`` runs the install hook as
   ``getattr(py_module, post_init)(env)`` where ``py_module`` is
   ``sys.modules['odoo.addons.<module>']`` -- the PACKAGE, not the
   ``hooks`` submodule. A hook function that lives only in ``hooks.py``
   and is never re-exported from ``__init__.py`` passes every static
   check, every upgrade, and every test on an already-installed
   database, then raises AttributeError on a FRESH INSTALL only.
   test_01 below is the assertion that guards this.

2. IDEMPOTENCE + THE SHIPPING POSTURE. The hook exists to seed defaults
   that the field-level defaults cannot reach (companies that already
   exist at install time) and to establish the module's shipping
   posture. Re-running it must reach the same fixed point.

   The posture: DRY-RUN-ARMED, not INERT. The hook ARMS the cron and
   sets ``lw_cc_surcharge_enabled=True`` instead of force-disabling.
   What the hook must still never do is turn dry-run OFF -- that is
   the line between "the cron reports every month" and "the cron
   charges customers", and it stays an operator act gated by
   ``_check_live_mode_requires_income_account``. It also arms the
   backend Pay-wizard flag (ships live) and must never arm the portal
   uplift or set a non-zero CC percentage.

Test class follows the TransactionCase + @tagged convention per
CL-Odoo-test-class-conventions-odoo19.
"""
import importlib

from odoo.modules.module import get_manifest
from odoo.tests import TransactionCase, tagged

# Imported from the PACKAGE top level on purpose: this import IS part
# of the assertion (see test_01). Importing from .hooks instead would
# still pass while the fresh-install path was broken.
from odoo.addons.lw_cc_surcharge import post_init_hook
from odoo.addons.lw_cc_surcharge.hooks import (
    CRON_XMLID,
    PRODUCT_XMLID,
    SC_MODE_DEFAULTS,
)

MODULE = 'lw_cc_surcharge'


@tagged('post_install', '-at_install', 'lw_cc_surcharge')
class TestPostInitHook(TransactionCase):

    def test_01_post_init_hook_resolvable_at_package_top_level(self):
        """The manifest's post_init_hook name resolves via getattr on
        the package, the way Odoo's own module loader resolves it."""
        hook_name = get_manifest(MODULE).get('post_init_hook')
        self.assertTrue(
            hook_name,
            "%s declares no post_init_hook in its manifest; this test "
            "and the module's install-time seeding assume one." % MODULE,
        )

        pkg = importlib.import_module('odoo.addons.' + MODULE)
        hook = getattr(pkg, hook_name, None)

        self.assertTrue(
            callable(hook),
            "%s: manifest post_init_hook %r does not resolve to a "
            "callable on the package top level. odoo/modules/loading.py "
            "calls getattr(sys.modules['odoo.addons.%s'], %r)(env) at "
            "install time, so the function must be re-exported from "
            "__init__.py, not merely defined in hooks.py."
            % (MODULE, hook_name, MODULE, hook_name),
        )
        # ...and it is the real hook, not a same-named stand-in.
        self.assertIs(hook, post_init_hook)

    def test_02_hook_reseeds_defaults_and_ships_dry_run_armed(self):
        """Re-running the hook re-seeds what it owns, arms the cron and
        the master switch, and never turns dry-run off."""
        company = self.env.company

        cron = self.env.ref(CRON_XMLID, raise_if_not_found=False)
        self.assertTrue(cron, "cron %s not found" % CRON_XMLID)
        product = self.env.ref(PRODUCT_XMLID, raise_if_not_found=False)
        self.assertTrue(product, "product %s not found" % PRODUCT_XMLID)

        policy_field = 'lw_cc_sc_partial_policy'
        self.assertIn(policy_field, SC_MODE_DEFAULTS)

        # Undo what the install-time hook did, and arrange the company
        # into the OLD inert state, so the assertions below discriminate
        # even on a database where the posture is already correct.
        cron.sudo().write({'active': False})
        company.sudo().write({
            'lw_cc_surcharge_product_id': False,
            policy_field: False,
            'lw_cc_surcharge_enabled': False,
            'lw_cc_surcharge_dry_run': True,
            'lw_cc_surcharge_cc_pct': 0.0,
            'lw_cc_surcharge_backend_wizard': False,
            'lw_cc_surcharge_portal_uplift': False,
        })
        self.assertFalse(cron.active)
        self.assertFalse(company.lw_cc_surcharge_product_id)
        self.assertFalse(company[policy_field])

        post_init_hook(self.env)

        # Cron is armed: the module ships DRY-RUN-ARMED.
        self.assertTrue(
            cron.active,
            "post_init_hook left the monthly service charge cron "
            "disabled; it must arrive armed (dry-run).",
        )
        self.assertTrue(
            cron.nextcall,
            "post_init_hook left the cron with no nextcall; an armed "
            "cron with no scheduled tick never runs.",
        )
        # Seeded values are restored.
        self.assertEqual(company.lw_cc_surcharge_product_id, product)
        self.assertEqual(
            company[policy_field], SC_MODE_DEFAULTS[policy_field],
            "post_init_hook did not re-seed %s; an unset value silently "
            "selects the OTHER branch in every consumer." % policy_field,
        )
        # The master switch is on -- the cron must have something to do.
        self.assertTrue(
            company.lw_cc_surcharge_enabled,
            "post_init_hook left lw_cc_surcharge_enabled off; the "
            "armed cron would find no enabled company and do nothing.",
        )
        # The backend Pay-wizard checkbox ships live...
        self.assertTrue(
            company.lw_cc_surcharge_backend_wizard,
            "post_init_hook left the backend Pay-wizard surcharge "
            "unarmed; it ships live.",
        )
        # ...but nothing that charges anyone. These are the safety
        # margin of the DRY-RUN-ARMED posture.
        self.assertTrue(
            company.lw_cc_surcharge_dry_run,
            "post_init_hook turned dry-run off; that is the line "
            "between reporting and charging customers, and only an "
            "operator may cross it.",
        )
        self.assertFalse(
            company.lw_cc_surcharge_cc_pct,
            "post_init_hook set a non-zero CC surcharge percentage.",
        )
        self.assertFalse(
            company.lw_cc_surcharge_portal_uplift,
            "post_init_hook armed the portal uplift; that flag "
            "ships INERT.",
        )

    def test_02b_hook_never_turns_dry_run_off(self):
        """Even starting from a live-looking company, the hook lands on
        dry-run. Forcing dry-run True is the safe direction and the
        hook must never do the reverse."""
        company = self.env.company
        income_account = self.env['account.account'].search(
            [('account_type', '=', 'income')], limit=1,
        )
        if not income_account:
            self.skipTest(
                "no income account on this database; live mode is "
                "unreachable, so there is nothing to test"
            )
        # Live mode is only reachable WITH an income account -- that is
        # _check_live_mode_requires_income_account doing its job, and
        # this release does not weaken it.
        company.sudo().write({
            'lw_cc_surcharge_income_account_id': income_account.id,
            'lw_cc_surcharge_enabled': True,
            'lw_cc_surcharge_dry_run': False,
        })
        self.assertFalse(company.lw_cc_surcharge_dry_run)

        post_init_hook(self.env)

        self.assertTrue(
            company.lw_cc_surcharge_dry_run,
            "post_init_hook left a company in live mode; the install "
            "posture is dry-run in every case.",
        )

    def test_03_hook_is_idempotent_on_a_second_run(self):
        """A second run of the hook is a no-op: it reaches a fixed
        point rather than drifting the configuration each time (the
        install path can re-run on a module reinstall)."""
        company = self.env.company
        cron = self.env.ref(CRON_XMLID, raise_if_not_found=False)
        self.assertTrue(cron, "cron %s not found" % CRON_XMLID)

        def _snapshot():
            values = [
                company.lw_cc_surcharge_product_id,
                company.lw_cc_surcharge_enabled,
                company.lw_cc_surcharge_cc_pct,
                company.lw_cc_surcharge_dry_run,
                company.lw_cc_surcharge_backend_wizard,
                company.lw_cc_surcharge_portal_uplift,
                cron.active,
            ]
            values.extend(company[name] for name in sorted(SC_MODE_DEFAULTS))
            return values

        post_init_hook(self.env)
        before = _snapshot()

        post_init_hook(self.env)
        after = _snapshot()

        self.assertEqual(
            before, after,
            "post_init_hook is not idempotent: a second run changed the "
            "company configuration or the cron state.",
        )
