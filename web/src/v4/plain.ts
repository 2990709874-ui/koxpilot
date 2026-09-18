/**
 * 人话词典。
 *
 * 两条规则同时成立：
 * 1. 不出现只有作者自己懂的内部口径（AST / 分位数 / parity / ground truth / 阈值版本…）；
 * 2. 也不写成聊天体。界面文案是**书面产品语**：名词化、陈述、可直接抄进方案文档。
 *
 * 所以这里不会出现：F1 / AUC / P95 / 分位数 / parity / ground truth / 双实现 / 哨兵 /
 * 阈值版本 / sha256 / ms / token。这些词属于工程文档，不属于给客户看的界面。
 */

import { AGE_BUCKETS, CATEGORY_ZH, PLATFORM_LABEL } from '../engine/taxonomy';

/** 国家码 → 中文国名。只覆盖数据集里出现的市场。 */
export const COUNTRY_ZH: Record<string, string> = {
  US: '美国', CA: '加拿大', GB: '英国', AU: '澳大利亚', DE: '德国', FR: '法国',
  ES: '西班牙', IT: '意大利', NL: '荷兰', SE: '瑞典', PL: '波兰', JP: '日本',
  KR: '韩国', TW: '中国台湾', HK: '中国香港', SG: '新加坡', MY: '马来西亚',
  ID: '印尼', VN: '越南', TH: '泰国', PH: '菲律宾', IN: '印度', PK: '巴基斯坦',
  BD: '孟加拉', BR: '巴西', MX: '墨西哥', AR: '阿根廷', CL: '智利', CO: '哥伦比亚',
  PE: '秘鲁', SA: '沙特', AE: '阿联酋', EG: '埃及', TR: '土耳其', IL: '以色列',
  ZA: '南非', NG: '尼日利亚', KE: '肯尼亚', RU: '俄罗斯', UA: '乌克兰',
};

export const country = (code: string): string => COUNTRY_ZH[code] ?? code;
export const platform = (code: string): string => PLATFORM_LABEL[code] ?? code;
export const category = (code: string): string => CATEGORY_ZH[code] ?? code;

/** 粉丝层级 → 人话。 */
export const TIER_ZH: Record<string, string> = {
  nano: '素人',
  micro: '微型',
  mid: '腰部',
  macro: '头部',
  mega: '超头部',
};

/** 粉丝层级 → 粉丝区间，鼠标悬停时说明。 */
export const TIER_RANGE: Record<string, string> = {
  nano: '1 千 – 1 万粉',
  micro: '1 万 – 10 万粉',
  mid: '10 万 – 50 万粉',
  macro: '50 万 – 200 万粉',
  mega: '200 万粉以上',
};

export const tier = (code: string): string => TIER_ZH[code] ?? code;

/** 投放目标 → 人话。 */
export const KPI_ZH: Record<string, string> = {
  conversion: '考核转化',
  engagement: '考核互动',
  reach: '考核曝光',
  balanced: '均衡考核',
};

export const gender = (g: string | null): string | null =>
  g === 'f' ? '以女性为主' : g === 'm' ? '以男性为主' : null;

/** 年龄段 → 人话。18-24 / 25-34 → 「18–34 岁」。 */
export function ageText(buckets: readonly string[]): string | null {
  if (buckets.length === 0) return null;
  const order = AGE_BUCKETS as readonly string[];
  const sorted = [...buckets].sort((a, b) => order.indexOf(a) - order.indexOf(b));
  const first = sorted[0]?.split('-')[0];
  const last = sorted[sorted.length - 1];
  if (sorted.length === 1) return last === '45+' ? '45 岁以上' : `${last} 岁`;
  if (last === '45+') return `${first} 岁以上`;
  return `${first}–${last?.split('-')[1]} 岁`;
}

/**
 * 四道检查 → 人话。
 *
 * 原来界面上写 G0/G1/G2/G3，只有作者自己知道那是什么。
 */
export const GATE_NAME: Record<string, string> = {
  G0: '资料完整性',
  G1: '数据真实性',
  G2: '人群匹配度',
  G3: '品牌安全',
};

export const GATE_ASK: Record<string, string> = {
  G0: '关键字段缺失的账号不进入后续评估；缺失过多时无法形成有效判定。',
  G1: '互动率、涨粉曲线、评论质量三项与同类账号对照，识别异常数据。',
  G2: '内容领域、语言与粉丝画像需与本次投放的目标人群一致。',
  G3: '排除高危内容、争议记录与近期竞品合作。',
};

/**
 * 每条规则 → 一句人话。
 *
 * 左边这些编号是内部规则号，界面上不出现，只用来查表。
 */
export const RULE_PLAIN: Record<string, string> = {
  'G0.1': '关键资料缺失',
  'G0.2': '资料缺失过多，无法形成有效判定',
  'G1.1': '互动率异常偏高',
  'G1.2': '互动率异常偏低',
  'G1.3': '评论与点赞比例异常',
  'G1.4': '播放量与粉丝量严重不匹配',
  'G1.5': '粉丝量突增且无内容支撑',
  'G1.6': '账号新建但涨粉速度异常',
  'G1.7': '评论重复或以表情为主',
  'G2.1': '自称领域与实际内容不一致',
  'G2.2': '多个数据源标签相互矛盾',
  'G2.3': '内容领域与本次投放品类不符',
  'G2.4': '内容语言与目标市场不符',
  'G2.5': '粉丝主要不在目标市场',
  'G2.6': '粉丝年龄性别结构与目标人群不符',
  'G3.1': '存在高危内容',
  'G3.2': '风险内容占比过高',
  'G3.3': '近期存在竞品合作',
  'G3.4': '存在争议记录',
  'G3.5': '属于受管制品类，需额外审核',
};

export const rulePlain = (ruleId: string): string => RULE_PLAIN[ruleId] ?? ruleId;

/** 判定 → 人话。 */
export const VERDICT_ZH: Record<string, string> = {
  pass: '可投放',
  review: '需人工复核',
  reject: '不予投放',
};

/** 三种买法 → 人话。界面上只出现这三个名字。 */
export const ARM_PLAIN: Record<string, { name: string; how: string }> = {
  koxpilot: { name: 'KOXPilot', how: '先完成四道筛查，再按单位预算的有效曝光效率分配' },
  follower_rank: { name: '按粉丝量投放', how: '按粉丝量降序签约，直至预算用尽 —— 行业最常见做法' },
  diversified_no_gate: { name: '结构分散但不筛查', how: '平台、地区、量级均衡铺开，不做账号质量筛查' },
};

export const armName = (arm: string): string => ARM_PLAIN[arm]?.name ?? arm;
export const armHow = (arm: string): string => ARM_PLAIN[arm]?.how ?? '';

/** 漏斗每一档 → 人话标题（key 来自流水线的 funnel step）。 */
export const FUNNEL_LABEL: Record<string, string> = {
  library: '达人库总量',
  recall: '符合平台、品类与市场要求',
  G0: '资料齐全',
  G1: '数据不是刷的',
  G2: '人群对得上',
  G3: '品牌安全',
};

export const funnelLabel = (key: string): string => FUNNEL_LABEL[key] ?? key;

/* ------------------------------------------------------------------ */
/* 证据的人话化                                                        */
/* ------------------------------------------------------------------ */

/** 信号名 → 人话。查不到的一律说「这项指标」，绝不把英文字段名甩到界面上。 */
export const SIGNAL_ZH: Record<string, string> = {
  completeness: '资料完整度',
  missing_critical_fields: '缺失的关键字段',
  engagement_rate: '互动率',
  view_follower_ratio: '播放量与粉丝量之比',
  comment_like_ratio: '评论与点赞之比',
  comment_dup_rate: '重复评论占比',
  comment_emoji_only_rate: '纯表情评论占比',
  follower_history: '涨粉曲线异常程度',
  followers_per_day: '日均涨粉',
  follower_growth_spike: '单月涨粉倍数',
  follower_growth_rate: '涨粉速度',
  fraud_score: '异常总分',
  jaccard: '自称领域与实际内容的重合度',
  geo_share: '目标国家粉丝占比',
  age_share: '目标年龄段粉丝占比',
  gender_share: '目标性别粉丝占比',
  authenticity_score: '真实性',
  declared_vs_observed_categories: '自称领域与实际内容的重合度',
  competitor_brand: '竞品合作记录',
  source_tags: '各数据源给的标签',
  campaign_fit: '与这次投放的贴合度',
  language: '内容语言',
  audience_geo: '目标国家粉丝占比',
  audience_age: '目标年龄段粉丝占比',
  audience_gender: '目标性别粉丝占比',
  audience_match: '人群匹配度',
  risk_severity_score: '风险内容严重度',
  risk_flags: '高危标记',
  competitor_overlap: '竞品合作',
  controversy: '争议历史',
};

const fmtNum = (x: number): string => {
  if (Math.abs(x) >= 1000) return x.toLocaleString('en-US', { maximumFractionDigits: 0 });
  if (Math.abs(x) >= 10) return x.toFixed(1);
  return x.toFixed(2);
};

/**
 * 一条证据 → 一句人话。
 *
 * 引擎自己的 `human_text` 带分位数、阈值来源、分组标签这些内部口径，那是给工程与审计看的；
 * 界面上只保留「实测值 + 同类账号参考线」这一层。
 */
export function reasonPlain(reason: {
  rule_id: string;
  signal: string;
  actual: number | string | null;
  threshold: number | string | null;
}): string {
  const what = SIGNAL_ZH[reason.signal] ?? '这项指标';
  const a = reason.actual;
  const t = reason.threshold;
  if (typeof a === 'number' && typeof t === 'number') {
    const asPct = Math.abs(a) <= 1 && Math.abs(t) <= 1;
    const fa = asPct ? `${(a * 100).toFixed(1)}%` : fmtNum(a);
    const ft = asPct ? `${(t * 100).toFixed(1)}%` : fmtNum(t);
    return `${what} ${fa}，同类账号参考线 ${ft}`;
  }
  if (typeof a === 'string' && a.trim() !== '') return `${what}：${a}`;
  if (typeof a === 'number') return `${what} ${fmtNum(a)}`;
  return what;
}

/**
 * 数据集里的 brief 名称是造数据时随手写的口语标签（"看转化"），
 * 产物字节一致性校验依赖原值，所以不改数据，只在展示时换成书面说法。
 */
const BRIEF_NAME_FIX: Array<[RegExp, string]> = [
  [/看转化/g, '考核转化'],
  [/看互动/g, '考核互动'],
  [/看曝光/g, '考核曝光'],
];

export function briefLabel(name: string): string {
  let out = name;
  for (const [re, to] of BRIEF_NAME_FIX) out = out.replace(re, to);
  return out;
}
