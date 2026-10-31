/** 作用：把字段名转成阅读标签，原始口径仍可在计算细节中核对；输入：定义文本；输出：友好文本。 */
export const fieldLabels: Record<string, string> = {
  arrival_delay_nonnegative: '非负到达延误分钟', arrival_delayed_15: '延误至少 15 分钟标记',
  arrival_delay: '到达时间偏差', cancelled: '取消标记', diverted: '备降标记', flight_date: '航班日期',
  carrier_id: '航空公司', origin_airport_id: '出发机场', dest_airport_id: '到达机场',
  delay_carrier: '航空公司原因分钟', delay_weather: '天气原因分钟', delay_nas: '空域系统原因分钟',
  delay_security: '安检原因分钟', delay_late_aircraft: '前序飞机晚到分钟',
  ArrDelay: '到达时间偏差', ArrDelayMinutes: '非负到达延误', ArrDel15: '是否延误至少 15 分钟',
  Cancelled: '是否取消', Diverted: '是否备降', Reporting_Airline: '航空公司代码',
};

/** 作用：只转换已知字段/集合称谓，不推断新规则；输入：原始定义；输出：中文阅读标签。 */
export function readableRule(text: string): string {
  let value = text;
  for (const [key, label] of Object.entries(fieldLabels).sort((a, b) => b[0].length - a[0].length)) {
    value = value.replace(new RegExp(`\\b${key}\\b`, 'g'), label);
  }
  return value.replace(/\bE\b/g, '有效到达航班').replace(/\bS\b/g, '全部计划航班').replace(/延误至少 15 分钟标记\s*=\s*1/g, '延误至少 15 分钟').replace(/([\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])/g, '$1');
}

/** 作用：用通俗文字表达已明确支持的筛选式；输入：原筛选；输出：等价标签或原文字。 */
export function readableFilter(value: string): string {
  const compact = value.replace(/\s/g, '').toLowerCase();
  const known: Record<string, string> = {
    'cancelled=0': '未取消', 'diverted=0': '未备降', 'arrival_delayisnotnull': '到达时间偏差完整',
    'arrival_delay_nonnegativeisnotnull': '非负延误分钟完整', 'arrival_delayed_15in(0,1)': '延误标记有效',
    'flight_date>=start_date': '从所选起始日期开始', 'flight_date<end_date': '截止日期当天不计入',
    'sameselectedcarrier/airport/groupscopefornumeratoranddenominator': '分子、分母使用相同的航空公司与机场范围',
  };
  return known[compact] ?? readableRule(value);
}
