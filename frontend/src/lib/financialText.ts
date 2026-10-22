const FINANCIAL_LABELS: Record<string, string> = {
  accounts_receivable: '应收账款',
  accounts_payable: '应付账款',
  inventory: '存货',
  total_assets: '总资产',
  total_liabilities: '总负债',
  revenue: '营业收入',
  net_profit: '净利润',
  net_profit_parent: '归母净利润',
  operating_cash_flow: '经营活动现金流净额',
}

export function financialLabel(value: string): string {
  return value.replace(/\b[a-z][a-z_]+\b/g, token => FINANCIAL_LABELS[token] || token)
}
