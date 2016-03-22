# -*- coding: utf-8 -*-
# © 2016 Incaser Informatica S.L. - Carlos Dauden
# License AGPL-3 - See http://www.gnu.org/licenses/agpl-3.0.html

from dateutil.relativedelta import relativedelta
import logging
import time

from openerp import api, exceptions, fields, models
from openerp.addons.decimal_precision import decimal_precision as dp
from openerp.exceptions import ValidationError
from openerp.tools.translate import _

_logger = logging.getLogger(__name__)


class AccountAnalyticInvoiceLine(models.Model):
    _name = "account.analytic.invoice.line"

    product_id = fields.Many2one(
        'product.product', string='Product', required=True)
    analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Analytic Account')
    name = fields.Text(string='Description', required=True)
    quantity = fields.Float(default=1.0, required=True)
    uom_id = fields.Many2one(
        'product.uom', string='Unit of Measure', required=True)
    price_unit = fields.Float('Unit Price', required=True)
    price_subtotal = fields.Float(
        compute='_compute_price_subtotal',
        digits_compute=dp.get_precision('Account'),
        string='Sub Total')
    discount = fields.Float(
        string='Discount (%)',
        digits=dp.get_precision('Discount'),
        copy=True,
        help='Discount that is applied in generated invoices.'
        ' It should be less or equal to 100')

    @api.multi
    @api.depends('quantity', 'price_unit', 'discount')
    def _compute_price_subtotal(self):
        for line in self:
            subtotal = line.quantity * line.price_unit
            discount = line.discount / 100
            subtotal *= 1 - discount
            if line.analytic_account_id.pricelist_id:
                cur = line.analytic_account_id.pricelist_id.currency_id
                line.price_subtotal = cur.round(subtotal)
            else:
                line.price_subtotal = subtotal

    @api.one
    @api.constrains('discount')
    def _check_discount(self):
        if self.discount > 100:
            raise ValidationError(_("Discount should be less or equal to 100"))

    @api.multi
    @api.onchange('product_id')
    def product_id_change(self):
        if not self.product_id:
            return {'domain': {'uom_id': []}}

        vals = {}
        domain = {'uom_id': [
            ('category_id', '=', self.product_id.uom_id.category_id.id)]}
        if not self.uom_id or (self.product_id.uom_id.category_id.id != self.uom_id.category_id.id):
            vals['uom_id'] = self.product_id.uom_id

        product = self.product_id.with_context(
            lang=self.analytic_account_id.partner_id.lang,
            partner=self.analytic_account_id.partner_id.id,
            quantity=self.quantity,
            date=self.analytic_account_id.recurring_next_date,
            pricelist=self.analytic_account_id.pricelist_id.id,
            uom=self.uom_id.id
        )

        name = product.name_get()[0][1]
        if product.description_sale:
            name += '\n' + product.description_sale
        vals['name'] = name

        vals['price_unit'] = product.price
        self.update(vals)
        return {'domain': domain}


class AccountAnalyticAccount(models.Model):
    _inherit = 'account.analytic.account'

    @api.model
    def _default_journal(self):
        company_id = self.env.context.get(
            'company_id', self.env.user.company_id.id)
        domain = [
            ('type', '=', 'sale'),
            ('company_id', '=', company_id)]
        return self.env['account.journal'].search(domain, limit=1)

    pricelist_id = fields.Many2one(
        comodel_name='product.pricelist',
        string='Pricelist')
    date_start = fields.Date(default=fields.Date.context_today)
    recurring_invoice_line_ids = fields.One2many(
        comodel_name='account.analytic.invoice.line',
        inverse_name='analytic_account_id',
        string='Invoice Lines')
    recurring_invoices = fields.Boolean(
        string='Generate recurring invoices automatically')
    recurring_rule_type = fields.Selection(
        [('daily', 'Day(s)'),
         ('weekly', 'Week(s)'),
         ('monthly', 'Month(s)'),
         ('yearly', 'Year(s)'),
         ],
        default='monthly',
        string='Recurrency',
        help="Invoice automatically repeat at specified interval")
    recurring_interval = fields.Integer(
        default=1,
        string='Repeat Every',
        help="Repeat every (Days/Week/Month/Year)")
    recurring_next_date = fields.Date(
        default=fields.Date.context_today,
        string='Date of Next Invoice')
    journal_id = fields.Many2one(
        'account.journal',
        string='Journal',
        default=_default_journal,
        domain="[('type', '=', 'sale'),('company_id', '=', company_id)]")

    def copy(self, default=None):
        # Reset next invoice date
        default['recurring_next_date'] = \
            self._defaults['recurring_next_date']()
        return super(AccountAnalyticAccount, self).copy(default=default)

    @api.onchange('recurring_invoices')
    def _onchange_recurring_invoices(self):
        if self.date_start and self.recurring_invoices:
            self.recurring_next_date = self.date_start

    @api.model
    def _insert_markers(self, line, date_start, date_end, date_format):
        line = line.replace('#START#', date_start.strftime(date_format))
        line = line.replace('#END#', date_end.strftime(date_format))
        return line

    @api.model
    def _prepare_invoice_line(self, line, invoice_id):
        product = line.product_id
        account_id = product.property_account_income_id.id or \
                     product.categ_id.property_account_income_categ_id.id
        contract = line.analytic_account_id
        fpos = contract.partner_id.property_account_position_id
        account_id = fpos.map_account(account_id)
        tax_id = fpos.map_tax(product.taxes_id)
        name = line.name
        if 'old_date' in self.env.context and 'next_date' in self.env.context:
            lang_obj = self.env['res.lang']
            contract = line.analytic_account_id
            lang = lang_obj.search(
                [('code', '=', contract.partner_id.lang)])
            date_format = lang.date_format or '%m/%d/%Y'
            name = self._insert_markers(
                name, self.env.context['old_date'],
                self.env.context['next_date'], date_format)
        return {
            'name': name,
            'account_id': account_id,
            'account_analytic_id': contract.id,
            'price_unit': line.price_unit,
            'quantity': line.quantity,
            'uos_id': line.uom_id.id,
            'product_id': line.product_id.id,
            'invoice_id': invoice_id,
            'invoice_line_tax_id': [(6, 0, tax_id)],
            'discount': line.discount,
        }

    @api.model
    def _prepare_invoice(self, contract):
        if not contract.partner_id:
            raise ValidationError(
                _('No Customer Defined!'),
                _("You must first select a Customer for Contract %s!") %
                contract.name)
        partner = contract.partner_id
        fpos = partner.property_account_position_id
        journal_ids = self.env['account.journal'].search(
            [('type', '=', 'sale'),
             ('company_id', '=', contract.company_id.id)],
            limit=1)
        if not journal_ids:
            raise ValidationError(
                _('Error!'),
                _('Please define a sale journal for the company "%s".') %
                (contract.company_id.name or '',))
        inv_data = {
            'reference': contract.code,
            'account_id': partner.property_account_receivable_id.id,
            'type': 'out_invoice',
            'partner_id': partner.id,
            'currency_id': partner.property_product_pricelist.currency_id.id,
            'journal_id': journal_ids.id,
            'date_invoice': contract.recurring_next_date,
            'origin': contract.name,
            'fiscal_position': fpos and fpos.id,
            'payment_term': partner.property_payment_term_id.id,
            'company_id': contract.company_id.id,
            'journal_id': contract.journal_id.id,
            'contract_id': contract.id,
        }
        # if contract.journal_id:
        #     inv_data['journal_id'] = contract.journal_id.id
        invoice = self.env['account.invoice'].create(inv_data)
        for line in contract.recurring_invoice_line_ids:
            invoice_line_vals = self._prepare_invoice_line(line, invoice.id)
            self.env['account.invoice.line'].create(invoice_line_vals)
        # invoice.button_compute()
        return invoice

    @api.model
    def recurring_create_invoice(self, automatic=False):
        current_date = time.strftime('%Y-%m-%d')
        contracts = self.search(
            [('recurring_next_date', '<=', current_date),
             ('account_type', '=', 'normal'),
             ('recurring_invoices', '=', True)])
        for contract in contracts:
            next_date = fields.Date.from_string(
                contract.recurring_next_date or fields.Date.today())
            interval = contract.recurring_interval
            old_date = next_date
            if contract.recurring_rule_type == 'daily':
                new_date = next_date + relativedelta(days=interval - 1)
            elif contract.recurring_rule_type == 'weekly':
                new_date = next_date + relativedelta(weeks=interval, days=-1)
            else:
                new_date = next_date + relativedelta(months=interval, days=-1)
            ctx = self.env.context.copy()
            ctx.update({
                'old_date': old_date,
                'next_date': new_date,
                # Force company for correct evaluate domain access rules
                'force_company': contract.company_id.id,
            })
            # Re-read contract with correct company
            contract = self.with_context(ctx).browse(contract.id)
            self.with_context(ctx)._prepare_invoice(contract)
            contract.write({
                'recurring_next_date': new_date.strftime('%Y-%m-%d')
            })
        return True
