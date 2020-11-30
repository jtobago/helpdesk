import uuid
import base64
from datetime import datetime
from odoo import _, api, fields, models, tools
from odoo.exceptions import AccessError


class HelpdeskTicket(models.Model):
    _name = "helpdesk.ticket"
    _description = "Helpdesk Ticket"
    _rec_name = "number"
    _order = "number desc"
    _inherit = ["mail.thread.cc", "mail.activity.mixin"]

    def _get_default_stage_id(self):
        return self.env["helpdesk.ticket.stage"].search([], limit=1).id

    @api.model
    def _read_group_stage_ids(self, stages, domain, order):
        stage_ids = self.env["helpdesk.ticket.stage"].search([])
        return stage_ids

    def _get_new_eid(self):
        u = uuid.uuid4()
        b = base64.b32encode(u.bytes).decode()
        return b[0:24]

    unique_eid = fields.Char(string="EID", size=32, default=_get_new_eid, readonly=True)

    number = fields.Char(string="Ticket number", default="/", readonly=True)
    name = fields.Char(string="Title", required=True)
    description = fields.Text(required=True)
    user_id = fields.Many2one(comodel_name="res.users", string="Assigned user")
    user_ids = fields.Many2many(
        comodel_name="res.users", related="team_id.user_ids", string="Users"
    )
    stage_id = fields.Many2one(
        comodel_name="helpdesk.ticket.stage",
        string="Stage",
        group_expand="_read_group_stage_ids",
        default=_get_default_stage_id,
        track_visibility="onchange",
        ondelete="restrict",
        index=True,
        copy=False,
    )
    partner_id = fields.Many2one(comodel_name="res.partner", string="Contact")
    partner_name = fields.Char()
    partner_email = fields.Char(string="Email")

    last_stage_update = fields.Datetime(
        string="Last Stage Update", default=fields.Datetime.now
    )
    assigned_date = fields.Datetime(string="Assigned Date")
    closed_date = fields.Datetime(string="Closed Date")
    time_to_respond = fields.Float(string="Time to Respond", copy=False)
    time_to_resolve = fields.Float(string="Time to Resolve", copy=False)
    closed = fields.Boolean(related="stage_id.closed")
    unattended = fields.Boolean(related="stage_id.unattended")
    tag_ids = fields.Many2many(comodel_name="helpdesk.ticket.tag", string="Tags")
    company_id = fields.Many2one(
        comodel_name="res.company",
        string="Company",
        required=True,
        default=lambda self: self.env.company,
    )
    channel_id = fields.Many2one(
        comodel_name="helpdesk.ticket.channel",
        string="Channel",
        help="Channel indicates where the source of a ticket"
        "comes from (it could be a phone call, an email...)",
    )
    category_id = fields.Many2one(
        comodel_name="helpdesk.ticket.category", string="Category",
    )
    team_id = fields.Many2one(comodel_name="helpdesk.ticket.team", string="Team",)
    priority = fields.Selection(
        selection=[
            ("0", _("Low")),
            ("1", _("Medium")),
            ("2", _("High")),
            ("3", _("Very High")),
        ],
        string="Priority",
        default="1",
    )
    attachment_ids = fields.One2many(
        comodel_name="ir.attachment",
        inverse_name="res_id",
        domain=[("res_model", "=", "helpdesk.ticket")],
        string="Media Attachments",
    )
    color = fields.Integer(string="Color Index")
    kanban_state = fields.Selection(
        selection=[
            ("normal", "Default"),
            ("done", "Ready for next stage"),
            ("blocked", "Blocked"),
        ],
        string="Kanban State",
    )
    active = fields.Boolean(default=True)

    def send_user_mail(self):
        self.env.ref("helpdesk_mgmt.assignment_email_template").send_mail(self.id)

    def send_team_mail(self):
        email_values = {
            'recipient_ids': [(4, partner.id) for partner in self.team_id.user_ids.mapped("partner_id")],
            'notification': True,
        }
        self.env.ref("helpdesk_mgmt.team_assignment_email_template").send_mail(self.id, email_values=email_values)

    def assign_to_me(self):
        self.write({"user_id": self.env.user.id})

    @api.onchange("partner_id")
    def _onchange_partner_id(self):
        if self.partner_id:
            self.partner_name = self.partner_id.name
            self.partner_email = self.partner_id.email

    @api.onchange("team_id", "user_id")
    def _onchange_dominion_user_id(self):
        if self.user_id and self.user_ids and self.user_id not in self.team_id.user_ids:
            self.update({"user_id": False})
            return {"domain": {"user_id": []}}
        if self.team_id:
            return {"domain": {"user_id": [("id", "in", self.user_ids.ids)]}}
        else:
            return {"domain": {"user_id": []}}

    # ---------------------------------------------------
    # CRUD
    # ---------------------------------------------------

    @api.model
    def create(self, vals):
        if vals.get("number", "/") == "/":
            seq = self.env["ir.sequence"]
            if "company_id" in vals:
                seq = seq.with_context(force_company=vals["company_id"])
            vals["number"] = seq.next_by_code("helpdesk.ticket.sequence") or "/"
        res = super().create(vals)

        # Check if mail to the user has to be sent
        if vals.get("user_id") and res:
            res.send_user_mail()
        if vals.get("team_id") and res:
            res.send_team_mail()
        return res

    def copy(self, default=None):
        self.ensure_one()
        if default is None:
            default = {}
        if "number" not in default:
            default["number"] = (
                self.env["ir.sequence"].next_by_code("helpdesk.ticket.sequence") or "/"
            )
        res = super().copy(default)
        return res

    def write(self, vals):
        for _ticket in self:
            now = fields.Datetime.now()
            if vals.get("stage_id"):
                stage = self.env["helpdesk.ticket.stage"].browse([vals["stage_id"]])
                vals["last_stage_update"] = now
                if stage.closed:
                    calendar = _ticket.company_id.resource_calendar_id
                    hours = calendar.get_work_hours_count(
                        _ticket.create_date, datetime.now(), compute_leaves=False)

                    vals["time_to_resolve"] = hours
                    vals["closed_date"] = now
            if vals.get("user_id"):
                vals["assigned_date"] = now

        res = super().write(vals)

        # Check if mail to the user has to be sent
        for ticket in self:
            if vals.get("user_id"):
                ticket.send_user_mail()
            if vals.get("team_id"):
                ticket.send_team_mail()
        return res

    def action_duplicate_tickets(self):
        for ticket in self.browse(self.env.context["active_ids"]):
            ticket.copy()

    # ---------------------------------------------------
    # Mail gateway
    # ---------------------------------------------------

    def _track_template(self, tracking):
        res = super()._track_template(tracking)
        ticket = self[0]
        if "stage_id" in tracking and ticket.stage_id.mail_template_id:
            res["stage_id"] = (
                ticket.stage_id.mail_template_id,
                {
                    "auto_delete_message": True,
                    "subtype_id": self.env["ir.model.data"].xmlid_to_res_id(
                        "mail.mt_note"
                    ),
                    "email_layout_xmlid": "mail.mail_notification_light",
                },
            )
        return res

    @api.returns('mail.message', lambda value: value.id)
    def message_post(self, **kwargs):
        if not self.time_to_respond and kwargs.get('message_type') == 'comment':
            calendar = self.company_id.resource_calendar_id
            hours = calendar.get_work_hours_count(self.create_date, datetime.now(), compute_leaves=False)

            self.write({'time_to_respond': hours})
        return super(HelpdeskTicket, self).message_post(**kwargs)

    @api.model
    def message_new(self, msg, custom_values=None):
        """ Override message_new from mail gateway so we can set correct
        default values.
        """
        if custom_values is None:
            custom_values = {}
        defaults = {
            "name": msg.get("subject") or _("No Subject"),
            "description": msg.get("body"),
            "partner_email": msg.get("from"),
            "partner_id": msg.get("author_id"),
        }
        defaults.update(custom_values)

        # Write default values coming from msg
        ticket = super().message_new(msg, custom_values=defaults)

        # Use mail gateway tools to search for partners to subscribe
        email_list = tools.email_split(
            (msg.get("to") or "") + "," + (msg.get("cc") or "")
        )
        partner_ids = [
            p.id
            for p in self.env["mail.thread"]._mail_find_partner_from_emails(
                email_list, records=ticket, force_create=False
            )
            if p
        ]
        ticket.message_subscribe(partner_ids)
        return ticket

    def message_update(self, msg, update_vals=None):
        """ Override message_update to subscribe partners """
        email_list = tools.email_split(
            (msg.get("to") or "") + "," + (msg.get("cc") or "")
        )
        partner_ids = [
            p.id
            for p in self.env["mail.thread"]._mail_find_partner_from_emails(
                email_list, records=self, force_create=False
            )
            if p
        ]
        self.message_subscribe(partner_ids)
        return super().message_update(msg, update_vals=update_vals)

    def _message_get_suggested_recipients(self):
        recipients = super()._message_get_suggested_recipients()
        try:
            for ticket in self:
                if ticket.partner_id:
                    ticket._message_add_suggested_recipient(
                        recipients, partner=ticket.partner_id, reason=_("Customer")
                    )
                elif ticket.partner_email:
                    ticket._message_add_suggested_recipient(
                        recipients,
                        email=ticket.partner_email,
                        reason=_("Customer Email"),
                    )
        except AccessError:
            # no read access rights -> just ignore suggested recipients because this
            # imply modifying followers
            pass
        return recipients
