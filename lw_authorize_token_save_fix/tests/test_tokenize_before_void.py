# -*- coding: utf-8 -*-
"""Tests for the tokenize-before-void fix (v19.0.1.0.0).

The validation void (the last step of a card-save attempt) now actually
succeeds against Authorize.Net: upstream ``payment_authorize``'s
``_send_void_request()`` sends the source transaction's provider
reference on the void. Once the void succeeds, Authorize.Net's
``createCustomerProfileFromTransactionRequest`` runs against an
already-voided transaction and returns no payment data -- the card is
never saved. ``models/payment_transaction.py`` overrides ``_void()`` to
tokenize from the still-active authorization first.

Covers:
- Call order: ``create_customer_profile`` fires before the void request.
- Token linkage: ``token_id`` set, ``provider_ref`` populated, ``tokenize``
  flipped to False.
- Idempotency: a second ``_tokenize({})`` call on an already-tokenized tx
  creates no second token and makes no second API call.
- Scope: a non-validation authorize tx going through ``_void()`` never
  calls ``create_customer_profile``.
- Degradation: a tokenize failure never blocks the void, and never
  propagates out of ``_void()``.

Driving mechanism: the full flow is driven through ``tx._process(...)``
(the same entry point ``_send_void_request`` itself calls when processing
the void response), matching the real call chain end to end -- confirmed
against actual Odoo 19 core source (``payment/models/payment_transaction.py``):
``_process()`` calls ``_validate_amount()`` before ``_apply_updates()``, but
``_validate_amount()`` skips entirely for ``operation == 'validation'``
transactions (our whole flow, including the child void tx, which inherits
``operation='validation'`` from ``_create_child_transaction``) -- so the
amount/``get_transaction_details`` path this suite was at risk of tripping
over never actually fires for tests 1/2/3/5. It DOES fire for test 4's
non-validation tx, which is why ``_neutralize_amount_validation`` patches
``_validate_amount`` to a no-op for every test regardless (confirmed present
under that exact name in v19 core); the mock's ``get_transaction_details``
stub is a second, belt-and-suspenders fallback in case that patch target
ever moves.

``AuthorizeAPI`` is patched at its ONE canonical definition site
(``authorize_request.AuthorizeAPI``), not at the name as re-imported into
``payment_transaction.py`` -- the exact pattern Odoo's own core
``payment_authorize`` test suite uses (``tests/test_authorize.py`` patches
``'...authorize_request.AuthorizeAPI.create_customer_profile'`` directly).
This is also just more robust than patching the re-imported name: it
covers every caller regardless of which module holds a reference to the
class, since every such import just binds a name to the one shared class
object -- relevant if ``lw_authorize_void_fix`` (a duplicate/no-op now
that core has the fix; not part of this branch) ever ends up installed
alongside this module, since it does its own separate ``from
...authorize_request import AuthorizeAPI`` for the void call.

Run with:
    odoo-bin -d <db> --test-enable --stop-after-init --workers=0 \\
        --no-http --test-tags /lw_authorize_token_save_fix
"""
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

_FIX_LOGGER = 'odoo.addons.lw_authorize_token_save_fix.models.payment_transaction'


@tagged('post_install', '-at_install', 'lw_authorize_token_save_fix')
class TestTokenizeBeforeVoid(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # payment_authorize ships its provider record under the shared
        # 'payment' xmlid namespace (every provider module's data record
        # lives there), same as every other provider (card, none, ...).
        cls.provider = cls.env.ref(
            'payment.payment_provider_authorize', raise_if_not_found=False,
        )
        if not cls.provider:
            cls.provider = cls.env['payment.provider'].search(
                [('code', '=', 'authorize')], limit=1,
            )
        cls.provider.write({
            'state': 'test',
            'authorize_login': 'test-login',
            'authorize_transaction_key': 'test-transaction-key',
            'authorize_signature_key': 'test-signature-key',
        })

        cls.method_card = cls.env.ref(
            'payment.payment_method_card', raise_if_not_found=False,
        )
        if not cls.method_card:
            cls.method_card = cls.env['payment.method'].create({
                'name': 'Card (test)',
                'code': 'card_authfix_test',
            })

        cls.partner = cls.env['res.partner'].create({
            'name': 'Authorize Tokenize-Before-Void Test Customer',
            'email': 'tokenize-before-void@example.com',
        })
        cls.currency = cls.env.company.currency_id

    def setUp(self):
        super().setUp()
        self._neutralize_amount_validation()

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _neutralize_amount_validation(self):
        """See module docstring: best-effort no-op for whatever
        amount-validation hook ``_process()`` may run before
        ``_apply_updates()``. Only patches if the attribute exists."""
        Tx = type(self.env['payment.transaction'])
        if hasattr(Tx, '_validate_amount'):
            patcher = patch.object(Tx, '_validate_amount', lambda *a, **kw: None)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _make_tx(self, operation='validation', tokenize=True, state='draft', **vals):
        create_vals = dict({
            'provider_id': self.provider.id,
            'payment_method_id': self.method_card.id,
            'partner_id': self.partner.id,
            'currency_id': self.currency.id,
            'amount': 0.01,  # Authorize.Net's own validation amount (penny auth).
            'reference': 'AUTHFIX-TEST-%s' % uuid4().hex[:12],
            'operation': operation,
            'tokenize': tokenize,
            'state': state,
        }, **vals)
        return self.env['payment.transaction'].create(create_vals)

    def _patch_authorize_api(self, create_profile_exc=None, void_response=None):
        """Patch AuthorizeAPI's methods directly on the class, at its one
        canonical definition site (authorize_request.AuthorizeAPI) -- the
        same pattern Odoo's own core payment_authorize test suite uses (see
        tests/test_authorize.py, which patches
        '...authorize_request.AuthorizeAPI.create_customer_profile' rather
        than the name as re-imported into payment_transaction.py). This
        covers every caller regardless of which module imported a reference
        to the class -- including lw_authorize_void_fix's own `from
        ...authorize_request import AuthorizeAPI` for the void call (see
        module docstring) -- since every such import just binds a name to
        the one shared class object.

        Returns (mock_api, call_order): mock_api exposes
        .create_customer_profile / .void / .get_transaction_details as the
        three patched Mocks (for call-count / assert_not_called
        assertions). call_order is appended to by both stubbed methods, in
        the order they're actually invoked, so tests can assert ordering
        directly.
        """
        call_order = []

        def _create_customer_profile(partner, transaction_id):
            call_order.append('create_customer_profile')
            if create_profile_exc is not None:
                raise create_profile_exc
            return {
                'profile_id': 'CP1',
                'payment_profile_id': 'PP1',
                'payment_details': '1111',
            }

        def _void(transaction_id):
            call_order.append('void')
            return void_response or {
                'x_response_code': '1',
                'x_trans_id': 'VOID1',
                'x_type': 'void',
                'payment_method_code': 'visa',
            }

        base = 'odoo.addons.payment_authorize.models.authorize_request.AuthorizeAPI'

        def _patch_method(name, side_effect=None, return_value=None):
            patcher = patch('%s.%s' % (base, name))
            mock_method = patcher.start()
            self.addCleanup(patcher.stop)
            if side_effect is not None:
                mock_method.side_effect = side_effect
            if return_value is not None:
                mock_method.return_value = return_value
            return mock_method

        mock_api = SimpleNamespace(
            create_customer_profile=_patch_method(
                'create_customer_profile', side_effect=_create_customer_profile,
            ),
            void=_patch_method('void', side_effect=_void),
            # Fallback safety net -- see module docstring (_validate_amount
            # is neutralized for every test, so this should never actually
            # fire).
            get_transaction_details=_patch_method(
                'get_transaction_details',
                return_value={'transaction': {'authAmount': '0.01'}},
            ),
        )
        return mock_api, call_order

    def _authorize_response(self, **overrides):
        response = {
            'x_response_code': '1',
            'x_trans_id': 'AUTH123',
            'x_type': 'auth_only',
            'payment_method_code': 'visa',
        }
        response.update(overrides)
        return {'response': response}

    # ------------------------------------------------------------------ #
    # Call order                                                          #
    # ------------------------------------------------------------------ #

    def test_tokenize_runs_before_void_request(self):
        """create_customer_profile (tokenize) must fire strictly before the
        void request, or Authorize.Net rejects
        createCustomerProfileFromTransactionRequest against an
        already-voided transaction (the bug this module fixes)."""
        tx = self._make_tx()
        _mock_api, call_order = self._patch_authorize_api()

        tx._process('authorize', self._authorize_response())

        self.assertEqual(
            call_order, ['create_customer_profile', 'void'],
            f"Expected tokenize before void; got call order: {call_order}",
        )

    # ------------------------------------------------------------------ #
    # Token linkage                                                       #
    # ------------------------------------------------------------------ #

    def test_token_created_and_linked(self):
        tx = self._make_tx()
        self._patch_authorize_api()

        tx._process('authorize', self._authorize_response())

        self.assertTrue(tx.token_id, "Validation tx should have a token linked.")
        self.assertEqual(tx.token_id.provider_ref, 'PP1')
        self.assertFalse(
            tx.tokenize,
            "_tokenize() must flip tokenize to False on success so the "
            "later _process()-driven tokenization is a no-op.",
        )

    # ------------------------------------------------------------------ #
    # Idempotency                                                         #
    # ------------------------------------------------------------------ #

    def test_retokenize_after_success_is_idempotent(self):
        tx = self._make_tx()
        mock_api, _call_order = self._patch_authorize_api()

        tx._process('authorize', self._authorize_response())
        self.assertTrue(tx.token_id)

        token_count_before = self.env['payment.token'].search_count(
            [('partner_id', '=', self.partner.id)],
        )
        create_profile_calls_before = mock_api.create_customer_profile.call_count

        # Calling _tokenize({}) again (as _process() would, redundantly, per
        # the module's own docstring) must be a silent no-op: token_id is
        # already set, so authorize's _extract_token_values() returns {}
        # without touching Authorize.Net at all.
        tx._tokenize({})

        self.assertEqual(
            self.env['payment.token'].search_count(
                [('partner_id', '=', self.partner.id)],
            ),
            token_count_before,
            "Re-tokenizing an already-tokenized tx must not create a "
            "second token.",
        )
        self.assertEqual(
            mock_api.create_customer_profile.call_count,
            create_profile_calls_before,
            "_extract_token_values() must short-circuit once token_id is "
            "set, without a second Authorize.Net API call.",
        )

    # ------------------------------------------------------------------ #
    # Scope                                                               #
    # ------------------------------------------------------------------ #

    def test_non_validation_tx_never_tokenizes_on_void(self):
        """A non-validation authorize tx going through _void() (e.g. a
        manual/partial void of a captured payment) must never trigger the
        tokenize-before-void path -- it only applies to validation txs."""
        tx = self._make_tx(operation='online_direct', tokenize=False, state='authorized')
        tx.provider_reference = 'ONLINE-REF-1'
        mock_api, _call_order = self._patch_authorize_api()

        tx._void()

        mock_api.create_customer_profile.assert_not_called()

    # ------------------------------------------------------------------ #
    # Degradation                                                         #
    # ------------------------------------------------------------------ #

    def test_tokenize_failure_does_not_block_void(self):
        """A tokenize failure is logged and swallowed; it must never block
        or interrupt the void, and must never propagate out of _void()."""
        tx = self._make_tx()
        _mock_api, call_order = self._patch_authorize_api(
            create_profile_exc=Exception('boom'),
        )

        with mute_logger(_FIX_LOGGER):
            tx._process('authorize', self._authorize_response())

        self.assertFalse(
            tx.token_id, "A failed tokenize attempt must not leave a token linked.",
        )
        self.assertIn(
            'void', call_order,
            "The void must still proceed despite the tokenize failure.",
        )
