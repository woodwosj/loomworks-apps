# -*- coding: utf-8 -*-
"""BIN (Bank Identification Number) record model for local card-type lookup.

Self-hosted BIN lookup. A CSV of BIN ranges is loaded into this model,
and the payment flow checks it to determine whether a card is debit or
credit before applying the CC surcharge.

Data source: bin-list-data GitHub project (free, open-source CSV).
Refresh: quarterly (BIN ranges shift slowly as banks reissue cards).

Performance (merged from the portal-uplift companion module): the
lookup tries the exact single-BIN row first, which is a plain index
equality (measured 0.02 ms), and only falls back to the containment
query when range rows actually exist, restricted to them through a
partial index. Against the real bin-list-data import (374k rows, every
one a single BIN where ``bin_start == bin_end``) a containment-only
query measured 38 ms for a hit and 79 ms for a miss -- the miss
scanning all 374,787 rows; an unknown card is the common case on a
customer-facing request.
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

_logger = logging.getLogger(__name__)

RANGE_INDEX = 'lw_cc_bin_record_range_only_idx'


class LwCcBINRecord(models.Model):
    _name = 'lw_cc.bin.record'
    _description = 'BIN (Bank Identification Number) Record'
    _order = 'bin_start'

    bin_start = fields.Char(
        string="BIN Start",
        required=True,
        index=True,
        help="Start of BIN range (first 6 digits of card number).",
    )
    bin_end = fields.Char(
        string="BIN End",
        required=True,
        help="End of BIN range (first 6 digits).",
    )
    card_type = fields.Selection(
        [('CREDIT', 'Credit'), ('DEBIT', 'Debit')],
        string="Card Type",
        required=True,
        help="Whether cards in this BIN range are credit or debit.",
    )
    network = fields.Char(
        string="Card Network",
        help="Card network: VISA, MASTERCARD, AMERICAN EXPRESS, DISCOVER, etc.",
    )
    issuer = fields.Char(
        string="Issuer",
        help="Issuing bank name.",
    )
    country_code = fields.Char(
        string="Country Code",
        size=2,
        help="ISO 3166-1 alpha-2 country code of the issuing bank.",
    )

    def init(self):
        super().init()
        # Partial index over range rows only. Keeps both the "are there any
        # ranges at all" probe and the containment fallback off the 374k
        # single-BIN rows.
        self.env.cr.execute(
            "SELECT indexname FROM pg_indexes WHERE indexname = %s",
            (RANGE_INDEX,),
        )
        if not self.env.cr.fetchone():
            self.env.cr.execute(
                "CREATE INDEX %s ON lw_cc_bin_record (bin_start, bin_end) "
                "WHERE bin_start <> bin_end" % RANGE_INDEX
            )
            _logger.info(
                "lw_cc_surcharge: created %s", RANGE_INDEX)

    @api.model
    def lookup_bin(self, bin_prefix):
        """Look up a BIN prefix and return the record, or None.

        Tries the exact single-BIN row first (plain index equality) and
        only falls back to the containment query when range rows exist
        (partial index), so both the common exact-match case and the
        unknown-card case stay cheap.

        :param str bin_prefix: First 6 digits of the card number.
        :returns: ``lw_cc.bin.record`` record or empty recordset.
        """
        if not bin_prefix or len(bin_prefix) < 6:
            return self.env['lw_cc.bin.record']

        prefix = bin_prefix[:6]
        exact = self.search(
            [('bin_start', '=', prefix), ('bin_end', '=', prefix)], limit=1)
        if exact:
            return exact

        # No exact row. Only pay for the containment query if range rows
        # exist at all; with a pure single-BIN dataset this probe returns
        # nothing off the partial index and the miss stays cheap.
        self.env.cr.execute(
            "SELECT id FROM lw_cc_bin_record "
            " WHERE bin_start <> bin_end AND bin_start <= %s AND bin_end >= %s"
            " ORDER BY bin_start LIMIT 1",
            (prefix, prefix),
        )
        row = self.env.cr.fetchone()
        return self.browse(row[0]) if row else self.browse()

    @api.model
    def is_debit(self, bin_prefix):
        """Check if a BIN prefix indicates a debit card.

        :returns: True if the card is debit, False if credit or unknown.
        """
        record = self.lookup_bin(bin_prefix)
        if record:
            return record.card_type == 'DEBIT'
        return False  # Unknown = credit (conservative: apply surcharge).

    @api.model
    def load_csv(self, csv_path):
        """Load BIN records from a CSV file.

        Expected CSV format (from bin-list-data):
        bin_start,bin_end,card_type,network,issuer,country_code

        This is a batch-load helper. The typical usage is:
        ``env['lw_cc.bin.record'].load_csv('/path/to/bins.csv')``
        called from a data loading script or cron.

        :param str csv_path: Absolute path to the CSV file.
        :returns: tuple (created_count, updated_count)
        """
        # System-only: this is an RPC-callable @api.model method that opens a
        # server-side path and rewrites the table deciding CREDIT vs DEBIT.
        # odoo-bin shell runs as superuser and passes.
        if not self.env.is_system():
            raise AccessError(_(
                "Only system administrators can load BIN reference data."
            ))
        import csv
        created = 0
        updated = 0

        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                start = row.get('bin_start', '').strip()
                end = row.get('bin_end', '').strip()
                if not start or not end:
                    continue

                existing = self.search([
                    ('bin_start', '=', start),
                    ('bin_end', '=', end),
                ], limit=1)

                vals = {
                    'bin_start': start,
                    'bin_end': end,
                    'card_type': row.get('card_type', 'CREDIT').strip().upper(),
                    'network': row.get('network', '').strip(),
                    'issuer': row.get('issuer', '').strip(),
                    'country_code': row.get('country_code', '').strip().upper(),
                }

                if existing:
                    existing.write(vals)
                    updated += 1
                else:
                    self.create(vals)
                    created += 1

        _logger.info(
            "lw_cc_bin_record: loaded BIN data from %s "
            "(%d created, %d updated).",
            csv_path, created, updated,
        )
        return (created, updated)
