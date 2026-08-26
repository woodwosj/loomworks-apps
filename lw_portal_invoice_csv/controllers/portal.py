import csv
import io

from odoo import http
from odoo.exceptions import AccessError, MissingError
from odoo.http import request
from odoo.addons.account.controllers.portal import PortalAccount


class PortalAccountCsv(PortalAccount):

    @http.route(
        ['/my/invoices/<int:invoice_id>/csv'],
        type='http',
        auth='public',
        website=True,
    )
    def portal_my_invoice_csv(self, invoice_id, access_token=None, **kw):
        """Return the invoice as CSV. Auth gate matches the PDF download:
        same access_token resolution via _document_check_access."""
        try:
            invoice_sudo = self._document_check_access(
                'account.move', invoice_id, access_token,
            )
        except (AccessError, MissingError):
            return request.redirect('/my')

        if invoice_sudo.state != 'posted':
            return request.redirect(
                f'/my/invoices/{invoice_id}'
                + (f'?access_token={access_token}' if access_token else '')
            )

        csv_bytes = self._build_invoice_csv(invoice_sudo)
        filename = self._build_invoice_csv_filename(invoice_sudo)
        headers = [
            ('Content-Type', 'text/csv; charset=utf-8'),
            ('Content-Length', str(len(csv_bytes))),
            ('Content-Disposition',
             f'attachment; filename="{filename}"'),
            ('X-Content-Type-Options', 'nosniff'),
        ]
        return request.make_response(csv_bytes, headers=headers)

    # ------------------------------------------------------------------
    # CSV builder — kept on the controller (not the model) so a custom
    # account.move override doesn't have to import the request object.
    # ------------------------------------------------------------------

    def _invoice_csv_header(self):
        return [
            'Line',
            'Internal Reference',
            'Barcode',
            'Product',
            'Description',
            'Quantity',
            'UoM',
            'Unit Price',
            'Discount %',
            'Taxes',
            'Subtotal',
            'Total',
        ]

    def _invoice_csv_row(self, line, seq):
        product = line.product_id
        return [
            seq,
            (product.default_code or '') if product else '',
            (product.barcode or '') if product else '',
            (product.display_name or '') if product else '',
            (line.name or '').replace('\n', ' ').strip(),
            f'{line.quantity:.4f}',
            (line.product_uom_id.name or '') if line.product_uom_id else '',
            f'{line.price_unit:.4f}',
            f'{(line.discount or 0.0):.2f}',
            ', '.join(line.tax_ids.mapped('name')),
            f'{line.price_subtotal:.2f}',
            f'{line.price_total:.2f}',
        ]

    def _build_invoice_csv(self, invoice):
        """Render product invoice lines (excl. note/section) to UTF-8 CSV bytes
        with leading metadata rows so the document is self-describing."""
        buf = io.StringIO()
        writer = csv.writer(buf)

        # Document header — small block of context for the receiver
        writer.writerow(['Company', invoice.company_id.name])
        writer.writerow(['Invoice', invoice.name or ''])
        writer.writerow(['Customer', invoice.partner_id.name or ''])
        writer.writerow(['Invoice Date',
                         invoice.invoice_date.isoformat() if invoice.invoice_date else ''])
        writer.writerow(['Due Date',
                         invoice.invoice_date_due.isoformat() if invoice.invoice_date_due else ''])
        writer.writerow(['Currency',
                         invoice.currency_id.name if invoice.currency_id else ''])
        writer.writerow(['Subtotal', f'{invoice.amount_untaxed:.2f}'])
        writer.writerow(['Tax', f'{invoice.amount_tax:.2f}'])
        writer.writerow(['Total', f'{invoice.amount_total:.2f}'])
        writer.writerow(['Amount Due', f'{invoice.amount_residual:.2f}'])
        writer.writerow([])  # blank separator

        # Line table
        writer.writerow(self._invoice_csv_header())

        product_lines = invoice.invoice_line_ids.filtered(
            lambda l: l.display_type not in ('line_section', 'line_note')
        ).sorted(key=lambda l: l.sequence)
        for idx, line in enumerate(product_lines, start=1):
            writer.writerow(self._invoice_csv_row(line, idx))

        return buf.getvalue().encode('utf-8')

    def _build_invoice_csv_filename(self, invoice):
        safe_name = (invoice.name or 'invoice').replace('/', '_').replace(' ', '_')
        date_part = invoice.invoice_date.isoformat() if invoice.invoice_date else 'undated'
        return f'{safe_name}_{date_part}.csv'
