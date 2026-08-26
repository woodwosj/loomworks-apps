# Retainer Hours Module
# Copyright (C) 2026 Loomworks LLC
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

from werkzeug.exceptions import Forbidden, NotFound

from odoo import http
from odoo.http import request


class RetainerPortalController(http.Controller):
    """Portal hours log for a retainer plan.

    Access is limited to portal users whose partner (or its parent/child
    within the same commercial partner) is the plan's client.
    """

    def _check_plan_access(self, plan):
        user_partner = request.env.user.partner_id
        commercial = user_partner.commercial_partner_id
        allowed = {
            user_partner.id,
            commercial.id,
        } | set(commercial.child_ids.ids)
        if plan.partner_id.id not in allowed:
            raise Forbidden()

    @http.route('/my/retainer/<int:plan_id>', type='http', auth='user',
                website=True)
    def portal_retainer_hours(self, plan_id, **kw):
        plan = request.env['retainer.plan'].browse(plan_id).exists()
        if not plan:
            raise NotFound()
        self._check_plan_access(plan)
        plan = plan.sudo()
        consumptions = plan.consumption_line_ids.sorted(
            key=lambda c: (c.date, c.id), reverse=True)
        return request.render('lw_retainer_hours.portal_retainer_hours', {
            'plan': plan,
            'consumptions': consumptions,
        })
