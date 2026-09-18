/**
 * 人话词典。
 *
 * 这一版的唯一规则：**页面上出现的每一个字，一个没做过技术的营销同学要能看懂。**
 * 看不懂的一律在这里翻译掉；翻译不出来的，说明它本来就不该出现在界面上（搬去 README）。
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
  micro: '小达人',
  mid: '腰部',
  macro: '大号',
  mega: '头部',
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
  conversion: '要转化',
  engagement: '要互动',
  reach: '要曝光',
  balanced: '均衡',
};

export const gender = (g: string | null): string | null =>
  g === 'f' ? '女性为主' : g === 'm' ? '男性为主' : null;

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
  G0: '资料齐不齐',
  G1: '数据是不是刷的',
  G2: '人群对不对得上',
  G3: '品牌安不安全',
};

export const GATE_ASK: Record<string, string> = {
  G0: '关键资料缺失的号先不投，缺太多连判断都做不了',
  G1: '互动、涨粉、评论三处对照同类账号，异常的挑出来',
  G2: '内容、语言、粉丝画像要跟这次要投的人对得上',
  G3: '高危内容、争议历史、最近接过竞品的号排掉',
};

/**
 * 每条规则 → 一句人话。
 *
 * 左边这些编号是内部规则号，界面上不出现，只用来查表。
 */
export const RULE_PLAIN: Record<string, string> = {
  'G0.1': '关键资料缺失',
  'G0.2': '资料缺得太多，判不准',
  'G1.1': '互动率高得不像真人',
  'G1.2': '互动率低到不正常',
  'G1.3': '评论和点赞的比例不对劲',
  'G1.4': '播放量远超粉丝量',
  'G1.5': '粉丝数出现断层式暴涨',
  'G1.6': '新号却在疯狂涨粉',
  'G1.7': '评论大量重复或只有表情',
  'G2.1': '自称的领域和实际内容不一样',
  'G2.2': '几个数据源给的标签互相矛盾',
  'G2.3': '内容和这次要投的品类不搭',
  'G2.4': '说的语言和目标市场不符',
  'G2.5': '粉丝主要不在目标国家',
  'G2.6': '粉丝的年龄性别和目标人群不符',
  'G3.1': '有高危内容',
  'G3.2': '风险内容太多',
  'G3.3': '最近接过竞品',
  'G3.4': '有过争议事件',
  'G3.5': '这是受管制品类，得额外审',
};

export const rulePlain = (ruleId: string): string => RULE_PLAIN[ruleId] ?? ruleId;

/** 判定 → 人话。 */
export const VERDICT_ZH: Record<string, string> = {
  pass: '可以投',
  review: '要人工确认',
  reject: '不建议投',
};

/** 三种买法 → 人话。界面上只出现这三个名字。 */
export const ARM_PLAIN: Record<string, { name: string; how: string }> = {
  koxpilot: { name: 'KOXPilot', how: '先过四道检查，再按「每块钱能买到多少真实播放」分预算' },
  follower_rank: { name: '按粉丝量买', how: '粉丝从多到少排，有钱就往下签——最常见的做法' },
  diversified_no_gate: { name: '只图分散不做检查', how: '平台、地区、量级都摊开，但不查账号真假' },
};

export const armName = (arm: string): string => ARM_PLAIN[arm]?.name ?? arm;
export const armHow = (arm: string): string => ARM_PLAIN[arm]?.how ?? '';

/** 漏斗每一档 → 人话标题（key 来自流水线的 funnel step）。 */
export const FUNNEL_LABEL: Record<string, string> = {
  library: '达人库全部账号',
  recall: '平台 / 品类 / 市场对得上',
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
  missing_critical_fields: '缺失的关键资料',
  engagement_rate: '互动率',
  view_follower_ratio: '播放量 / 粉丝量',
  comment_like_ratio: '评论 / 点赞',
  comment_dup_rate: '重复评论占比',
  comment_emoji_only_rate: '纯表情评论占比',
  follower_history: '涨粉曲线的异常程度',
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
 * 引擎自己的 `human_text` 里带分位数、阈值来源、组标签这些内部口径，
 * 那是写给工程和审计看的；界面上只说「实测多少、同类账号的参考线是多少」。
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
    return `${what} ${fa}，同类账号的参考线是 ${ft}`;
  }
  if (typeof a === 'string' && a.trim() !== '') return `${what}：${a}`;
  if (typeof a === 'number') return `${what} ${fmtNum(a)}`;
  return what;
}
