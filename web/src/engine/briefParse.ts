/**
 * 自由输入 brief 的**纯规则**解析器：任意一段中文投放需求 → `CampaignSpec`。
 *
 * 为什么需要它（也是这个文件唯一的存在理由）：
 * A1 BriefAgent 在构建期是真调 LLM 的（token 账见 `output/llm_bench.json`），
 * 但线上是纯静态部署，不暴露 key、不打任何 endpoint，所以 A1 的结果是固化产物。
 * 后果是读者只能在 3 个预置 brief 里选，无法自己敲一句话进去 ——
 * 于是「这前端是不是只是个壳」这个怀疑没法当场被推翻。
 *
 * 这个解析器补的就是这一刀：**线上用规则把自然语言解析成 spec，然后让 A2~A6
 * 在浏览器里真的重算一遍**。它不是 LLM，也不假装是 LLM：
 * - 每个字段都返回命中证据（命中了原文的哪个片段、用了哪条规则 id）；
 * - 抽不出来的字段如实降级为默认值，并标 `status = 'default'`；
 * - 由规则推导（而非原文直接命中）的字段标 `status = 'derived'`，不混进「原文命中」；
 * - `diffSpec()` 把同一条 brief 的「构建期 LLM 解析」与「线上规则解析」逐字段对撞，
 *   差异照实量化，不做四舍五入的漂亮话。
 *
 * 纪律：本文件**不参与** Python/TS 双实现一致性校验，也不被 `engine.ts` / `g0~g3.ts`
 * 引用，只依赖 `taxonomy.ts` 的品类/市场/竞品词表与 `types.ts` 的 spec 契约。
 * 它是额外能力，加进来不影响 consistency.json 的 0 差异。
 */

import {
  AGE_BUCKETS,
  CATEGORY_ADJACENCY,
  CATEGORY_ZH,
  COMPETITOR_GROUPS,
  COUNTRY_LANG,
} from './taxonomy.ts';
import type { CampaignSpec } from './types.ts';

/* ------------------------------------------------------------------ */
/* 证据结构                                                            */
/* ------------------------------------------------------------------ */

export type EvidenceStatus = 'hit' | 'derived' | 'default';

/** 一个字段的解析证据。页面上逐行渲染它，就是「真的在解析」的可核对凭据。 */
export interface FieldEvidence {
  field: keyof CampaignSpec;
  /** 中文字段名，给人看 */
  label: string;
  /** hit = 原文直接命中；derived = 由命中项按业务规则推导；default = 没抽出来，用默认值 */
  status: EvidenceStatus;
  /** 规则 id，和下面 RULES 表一一对应，可追溯 */
  rule: string;
  /** 规则的一句话说明 */
  ruleText: string;
  /** 命中的原文片段（default 时为空串） */
  matched: string;
  /** 片段在原文中的起始下标，-1 表示不来自原文 */
  at: number;
  /** 解析出的值（展示用字符串） */
  value: string;
}

export interface BriefParseResult {
  spec: CampaignSpec;
  evidence: FieldEvidence[];
  /** 友好提示：哪些关键字段没识别到、会导致什么后果 */
  warnings: string[];
  /** 真实解析耗时（performance.now()），和页面上其它耗时同源 */
  elapsedMs: number;
}

/** 规则表：id → 一句话说明。页面上展示 rule id 时用它取文案。 */
export const RULES: Record<string, string> = {
  'R-BUD-DOLLAR': '$ 前缀数字（含千分位），可带 万/k/m 量级后缀',
  'R-BUD-CN-UNIT': '数字 + 量级词（万/千/k/m）+ 货币词（美元/美金/USD）',
  'R-BUD-NEAR': '「预算」二字后 8 字符内的第一个数字（量级词可选，无货币词时按美元计）',
  'R-CAT-KEYWORD': '品类关键词表命中（覆盖 taxonomy 的 8 个一级品类）',
  'R-CAT-ADJ': '命中品类不足 2 个时，按 CATEGORY_ADJACENCY 补一个相邻品类（规则推导，非原文命中）',
  'R-PLAT-ALIAS': '平台名与中文别名表命中（抖音国际版 / 油管 / ins 等）',
  'R-MKT-COUNTRY': '国家/地区名命中，映射到 ISO 国家码',
  'R-MKT-REGION': '区域词（北美/东南亚/拉美/中东/日韩…）展开为国家码集合',
  'R-MKT-REGION-YIELD': '原文同时出现区域词与具体国家时，以具体国家为准，区域词只记录不展开',
  'R-LANG-DERIVE': '按 COUNTRY_LANG 从目标市场派生语言集，并追加 en（出海通行语）',
  'R-KPI-WORD': 'KPI 关键词命中：转化/成交 → conversion，曝光/量/安装 → reach，互动/口碑/种草 → engagement',
  'R-GENDER-WORD': '性别词命中：女性/女生/宝妈 → f，男性/男生 → m',
  'R-AGE-RANGE': '「a-b 岁」区间与 AGE_BUCKETS 求交集（有重叠即纳入）',
  'R-AGE-WORD': '人群词命中：青少年/学生 → 18-24，宝妈/职场 → 25-34',
  'R-COMP-BRAND': '竞品词表（COMPETITOR_GROUPS 的 25 个品牌）在回避语境中命中',
  'R-REG-WORD': '受管制口径词命中：儿童向/青少年 → kids，医疗/功效 → medical，金融/理财 → finance',
  'R-DEFAULT': '未识别，用默认值（已在证据里标注，不假装识别到）',
};

/* ------------------------------------------------------------------ */
/* 词表                                                               */
/* ------------------------------------------------------------------ */

/** 品类关键词。key 与 taxonomy.CATEGORIES 完全一致，避免出现引擎不认识的品类。 */
const CATEGORY_WORDS: Record<string, string[]> = {
  '3c_digital': ['3c', '数码', '电子产品', '手机', '耳机', '充电', '笔记本', '相机', '智能硬件', '平板'],
  home_appliance: ['小家电', '家电', '家居', '厨电', '扫地机', '吸尘器', '清洁电器', '厨房', '生活电器'],
  beauty_care: ['彩妆', '美妆', '护肤', '个护', '化妆', '香水', '口红', '面膜', '美容'],
  fashion: ['服饰', '服装', '鞋', '箱包', '穿搭', '时尚', '快时尚', '内衣', '配饰'],
  mother_baby: ['母婴', '婴儿', '宝宝', '奶粉', '纸尿裤', '童装', '儿童用品'],
  food_health: ['食品', '零食', '保健', '营养', '膳食', '健身补剂', '饮料', '咖啡'],
  gaming_app: ['手游', '游戏', '电竞', 'app', '应用', '小游戏', '休闲游戏', '独立游戏'],
  auto_travel: ['汽车', '出行', '旅行', '旅游', '电动车', '新能源车', '摩托', '户外出行'],
};

/** 平台别名。value 必须是 taxonomy.PLATFORMS 里的 id。 */
const PLATFORM_WORDS: Record<string, string[]> = {
  tiktok: ['tiktok', 'tik tok', 'tt', '抖音国际版', '国际版抖音', '抖音'],
  youtube: ['youtube', 'yt', '油管', '优管'],
  instagram: ['instagram', 'ins', 'ig', '照片墙'],
  x: ['twitter', ' x '],
  kwai: ['kwai', '快手国际版'],
};

/** 国家/地区名 → ISO 码。长词优先匹配（「马来西亚」要早于「马来」）。 */
const COUNTRY_WORDS: Array<[string, string]> = [
  ['美国', 'US'], ['加拿大', 'CA'], ['英国', 'GB'], ['澳大利亚', 'AU'], ['澳洲', 'AU'],
  ['德国', 'DE'], ['法国', 'FR'], ['西班牙', 'ES'], ['意大利', 'IT'], ['荷兰', 'NL'],
  ['瑞典', 'SE'], ['波兰', 'PL'], ['日本', 'JP'], ['韩国', 'KR'], ['台湾', 'TW'],
  ['香港', 'HK'], ['新加坡', 'SG'], ['马来西亚', 'MY'], ['马来', 'MY'], ['印尼', 'ID'],
  ['印度尼西亚', 'ID'], ['越南', 'VN'], ['泰国', 'TH'], ['菲律宾', 'PH'], ['印度', 'IN'],
  ['巴西', 'BR'], ['墨西哥', 'MX'], ['阿根廷', 'AR'], ['智利', 'CL'], ['哥伦比亚', 'CO'],
  ['秘鲁', 'PE'], ['沙特', 'SA'], ['阿联酋', 'AE'], ['迪拜', 'AE'], ['埃及', 'EG'],
  ['土耳其', 'TR'], ['以色列', 'IL'], ['南非', 'ZA'], ['尼日利亚', 'NG'], ['俄罗斯', 'RU'],
];

/** 区域词 → 国家码集合。 */
const REGION_WORDS: Array<[string, string[]]> = [
  ['北美', ['US', 'CA']],
  ['东南亚', ['ID', 'MY', 'VN', 'TH', 'PH', 'SG']],
  ['日韩', ['JP', 'KR']],
  ['拉美', ['BR', 'MX', 'AR']],
  ['南美', ['BR', 'AR', 'CL']],
  ['中东', ['SA', 'AE', 'EG']],
  ['西欧', ['GB', 'DE', 'FR', 'NL']],
  ['欧洲', ['GB', 'DE', 'FR', 'ES', 'IT', 'NL']],
  ['大中华', ['TW', 'HK']],
  ['港台', ['TW', 'HK']],
];

const KPI_WORDS: Array<[string, string[]]> = [
  ['conversion', ['转化', '成交', '下单', '出单', 'roi', 'roas', '销量', '带货']],
  ['engagement', ['互动', '口碑', '评论', '种草', '讨论', '话题度', '社区']],
  ['reach', ['曝光', '触达', '声量', '安装', '拉新量', '先要量', 'reach', '播放量']],
];

const REGULATED_WORDS: Array<[string, string[]]> = [
  ['kids', ['儿童向', '儿童', '青少年', '低龄', '未成年']],
  ['medical', ['医疗', '医美', '功效宣称', '保健功效', '药', '械字号']],
  ['finance', ['金融', '理财', '借贷', '加密', '博彩']],
];

/** 竞品品牌全表（从 COMPETITOR_GROUPS 摊平，不另维护一份，避免漂移）。 */
const ALL_BRANDS: string[] = COMPETITOR_GROUPS.flat();

/** 回避语境词：命中品牌时要求附近出现其中之一，避免把「我们是 Anker」当成回避。 */
const AVOID_WORDS = ['避开', '避免', '回避', '不要', '排除', '不能', '剔除', '不合作', '规避'];

/* ------------------------------------------------------------------ */
/* 工具                                                               */
/* ------------------------------------------------------------------ */

/** 全角转半角 + 小写，供关键词匹配用；下标与原文一一对应（等长替换）。 */
function normalize(text: string): string {
  return text
    .replace(/[\uff01-\uff5e]/g, (ch) => String.fromCharCode(ch.charCodeAt(0) - 0xfee0))
    .replace(/\u3000/g, ' ')
    .toLowerCase();
}

/** 取原文片段做证据展示（带上下文，最长 24 字）。 */
function snippet(raw: string, at: number, len: number): string {
  const s = Math.max(0, at - 4);
  const e = Math.min(raw.length, at + len + 6);
  return `${s > 0 ? '…' : ''}${raw.slice(s, e).trim()}${e < raw.length ? '…' : ''}`;
}

function uniq<T>(xs: T[]): T[] {
  return [...new Set(xs)];
}

/* ------------------------------------------------------------------ */
/* 各字段解析                                                          */
/* ------------------------------------------------------------------ */

const MULTIPLIER: Record<string, number> = {
  万: 1e4, w: 1e4, 千: 1e3, k: 1e3, m: 1e6, 百万: 1e6, 亿: 1e8,
};

interface BudgetHit {
  usd: number;
  rule: string;
  at: number;
  len: number;
}

/** 预算：认「8 万美元」「$80,000」「80000 美元」「12 万」「5w」等写法。 */
function parseBudget(low: string): BudgetHit | null {
  const patterns: Array<[string, RegExp]> = [
    ['R-BUD-DOLLAR', /\$\s*([\d,]+(?:\.\d+)?)\s*(万|w|k|m)?/],
    ['R-BUD-CN-UNIT', /([\d,]+(?:\.\d+)?)\s*(万|w|k|m|千|百万)?\s*(?:美元|美金|usd|dollars?)/],
    ['R-BUD-NEAR', /预算[^\d]{0,8}([\d,]+(?:\.\d+)?)\s*(万|w|k|m|千|百万)?/],
  ];
  for (const [rule, re] of patterns) {
    const m = re.exec(low);
    if (!m) continue;
    const n = Number(m[1].replace(/,/g, ''));
    if (!Number.isFinite(n) || n <= 0) continue;
    const mult = m[2] ? (MULTIPLIER[m[2]] ?? 1) : 1;
    const usd = n * mult;
    // 明显不是金额（比如把「25-40 岁」误吃进来）时放弃这条规则
    if (usd < 100) continue;
    return { usd, rule, at: m.index, len: m[0].length };
  }
  return null;
}

interface WordHit {
  key: string;
  word: string;
  at: number;
}

function scanWords(low: string, table: Record<string, string[]>): WordHit[] {
  const hits: WordHit[] = [];
  for (const [key, words] of Object.entries(table)) {
    for (const w of words) {
      const at = low.indexOf(w);
      if (at >= 0) {
        hits.push({ key, word: w.trim(), at });
        break;
      }
    }
  }
  return hits.sort((a, b) => a.at - b.at);
}

/** 年龄区间 → AGE_BUCKETS 交集。有重叠即纳入（宁可多召回一档，也不悄悄丢人群）。 */
const BUCKET_RANGE: Record<string, [number, number]> = {
  '18-24': [18, 24],
  '25-34': [25, 34],
  '35-44': [35, 44],
  '45+': [45, 200],
};

function bucketsForRange(lo: number, hi: number): string[] {
  return AGE_BUCKETS.filter((b) => {
    const [a, z] = BUCKET_RANGE[b];
    return lo <= z && hi >= a;
  });
}

/* ------------------------------------------------------------------ */
/* 主入口                                                             */
/* ------------------------------------------------------------------ */

/** 默认值集中在这里，方便页面上把「哪些字段是兜底的」讲清楚。 */
export const PARSE_DEFAULTS = {
  budget_usd: 50000,
  kpi: 'balanced',
  markets: ['US'] as string[],
  categories: [] as string[],
};

export function parseBrief(
  rawText: string,
  options: { campaignId?: string; name?: string } = {},
): BriefParseResult {
  const t0 = typeof performance !== 'undefined' ? performance.now() : Date.now();
  const raw = rawText ?? '';
  const low = normalize(raw);
  const ev: FieldEvidence[] = [];
  const warnings: string[] = [];

  const push = (
    field: keyof CampaignSpec,
    label: string,
    status: EvidenceStatus,
    rule: string,
    at: number,
    len: number,
    value: string,
  ): void => {
    ev.push({
      field,
      label,
      status,
      rule,
      ruleText: RULES[rule] ?? rule,
      matched: status === 'default' || at < 0 ? '' : snippet(raw, at, len),
      at: status === 'default' ? -1 : at,
      value,
    });
  };

  /* ---- 预算 ---- */
  const b = parseBudget(low);
  const budget = b ? Math.round(b.usd) : PARSE_DEFAULTS.budget_usd;
  if (b) {
    push('budget_usd', '预算', 'hit', b.rule, b.at, b.len, `$${budget.toLocaleString('en-US')}`);
  } else {
    push('budget_usd', '预算', 'default', 'R-DEFAULT', -1, 0, `$${budget.toLocaleString('en-US')}（未识别，用默认值）`);
    warnings.push('没识别到预算金额，按默认 $50,000 计算 —— 试试写「预算 8 万美元」或「$80,000」。');
  }

  /* ---- 品类 ---- */
  const catHits = scanWords(low, CATEGORY_WORDS);
  let categories = uniq(catHits.map((h) => h.key));
  for (const h of catHits) {
    push('target_categories', '品类', 'hit', 'R-CAT-KEYWORD', h.at, h.word.length, `${CATEGORY_ZH[h.key] ?? h.key}（${h.key}）`);
  }
  if (categories.length === 1) {
    // LLM 版同样会把一个相邻品类纳入召回面（相邻表是公开业务常量），这里按同一口径补，
    // 但标成 derived，明确它不是原文命中。
    const adj = (CATEGORY_ADJACENCY[categories[0]] ?? [])[0];
    if (adj) {
      categories = [...categories, adj];
      push('target_categories', '品类（相邻补全）', 'derived', 'R-CAT-ADJ', -1, 0, `${CATEGORY_ZH[adj] ?? adj}（${adj}）`);
    }
  }
  if (categories.length === 0) {
    push('target_categories', '品类', 'default', 'R-DEFAULT', -1, 0, '不限品类（未识别）');
    warnings.push('没识别到品类，A2 召回不会做品类硬筛选，候选池会明显偏大 —— 试试写「3C 小家电」「彩妆」「休闲手游」。');
  }

  /* ---- 平台 ---- */
  const platHits = scanWords(low, PLATFORM_WORDS);
  const platforms = uniq(platHits.map((h) => h.key));
  for (const h of platHits) {
    push('platforms', '平台', 'hit', 'R-PLAT-ALIAS', h.at, h.word.length, h.key);
  }
  if (platforms.length === 0) {
    push('platforms', '平台', 'default', 'R-DEFAULT', -1, 0, '不限平台（未识别）');
    warnings.push('没识别到平台，按「不限平台」处理。');
  }

  /* ---- 市场 ---- */
  const countryHits: WordHit[] = [];
  for (const [word, code] of COUNTRY_WORDS) {
    const at = low.indexOf(word);
    if (at >= 0 && !countryHits.some((h) => h.key === code)) countryHits.push({ key: code, word, at });
  }
  countryHits.sort((a, b2) => a.at - b2.at);
  const regionHits = REGION_WORDS.map(([word, codes]) => ({ word, codes, at: low.indexOf(word) })).filter((r) => r.at >= 0);

  let markets: string[] = [];
  if (countryHits.length > 0) {
    markets = uniq(countryHits.map((h) => h.key));
    for (const h of countryHits) push('target_markets', '市场', 'hit', 'R-MKT-COUNTRY', h.at, h.word.length, h.key);
    for (const r of regionHits) {
      push(
        'target_markets',
        '市场（区域词让位）',
        'derived',
        'R-MKT-REGION-YIELD',
        r.at,
        r.word.length,
        `识别到「${r.word}」，但原文已列明具体国家，按具体国家为准（未展开 ${r.codes.join('/')}）`,
      );
    }
  } else if (regionHits.length > 0) {
    markets = uniq(regionHits.flatMap((r) => r.codes));
    for (const r of regionHits) {
      push('target_markets', '市场（区域展开）', 'hit', 'R-MKT-REGION', r.at, r.word.length, `${r.word} → ${r.codes.join('/')}`);
    }
  } else {
    markets = [...PARSE_DEFAULTS.markets];
    push('target_markets', '市场', 'default', 'R-DEFAULT', -1, 0, 'US（未识别，用默认值）');
    warnings.push('没识别到市场，按默认 US 处理 —— 试试写「北美」「东南亚」或直接点国家名。');
  }

  /* ---- 语言（从市场派生） ---- */
  const languages = uniq([...markets.map((m) => COUNTRY_LANG[m]).filter(Boolean), 'en']);
  push('target_languages', '语言', 'derived', 'R-LANG-DERIVE', -1, 0, languages.join('/'));

  /* ---- KPI ---- */
  let kpi = PARSE_DEFAULTS.kpi;
  let kpiHit: { at: number; word: string } | null = null;
  for (const [key, words] of KPI_WORDS) {
    for (const w of words) {
      const at = low.indexOf(w);
      if (at >= 0 && (kpiHit === null || at < kpiHit.at)) {
        kpi = key;
        kpiHit = { at, word: w };
      }
    }
  }
  if (kpiHit) push('kpi', 'KPI', 'hit', 'R-KPI-WORD', kpiHit.at, kpiHit.word.length, kpi);
  else push('kpi', 'KPI', 'default', 'R-DEFAULT', -1, 0, 'balanced（未识别，用默认值）');

  /* ---- 性别 ---- */
  const GENDER_WORDS: Array<[string, string[]]> = [
    ['f', ['女性', '女生', '女孩', '宝妈', '女用户', '妈妈']],
    ['m', ['男性', '男生', '男孩', '男用户']],
  ];
  let gender: string | null = null;
  for (const [key, words] of GENDER_WORDS) {
    for (const w of words) {
      const at = low.indexOf(w);
      if (at >= 0) {
        gender = key;
        push('target_gender', '性别', 'hit', 'R-GENDER-WORD', at, w.length, key === 'f' ? '女性 f' : '男性 m');
        break;
      }
    }
    if (gender) break;
  }
  if (!gender) push('target_gender', '性别', 'default', 'R-DEFAULT', -1, 0, '不限性别（未识别）');

  /* ---- 年龄 ---- */
  let ageBuckets: string[] = [];
  const ageRange = /(\d{2})\s*[-~—～到至]\s*(\d{2})\s*(?:岁|years?)?/.exec(low);
  if (ageRange && Number(ageRange[1]) >= 10 && Number(ageRange[2]) <= 99) {
    const lo = Number(ageRange[1]);
    const hi = Number(ageRange[2]);
    ageBuckets = bucketsForRange(lo, hi);
    push(
      'target_age_buckets',
      '年龄',
      'hit',
      'R-AGE-RANGE',
      ageRange.index,
      ageRange[0].length,
      `${lo}-${hi} 岁 → ${ageBuckets.join('/')}`,
    );
  } else {
    const AGE_HINT: Array<[string, string[]]> = [
      ['18-24', ['青少年', '学生', 'z 世代', 'z世代', '年轻人']],
      ['25-34', ['宝妈', '职场', '白领', '新手父母']],
    ];
    for (const [bucket, words] of AGE_HINT) {
      for (const w of words) {
        const at = low.indexOf(w);
        if (at >= 0) {
          ageBuckets = [bucket];
          push('target_age_buckets', '年龄', 'hit', 'R-AGE-WORD', at, w.length, `${w} → ${bucket}`);
          break;
        }
      }
      if (ageBuckets.length) break;
    }
  }
  if (ageBuckets.length === 0) push('target_age_buckets', '年龄', 'default', 'R-DEFAULT', -1, 0, '不限年龄（未识别）');

  /* ---- 竞品回避 ---- */
  const competitors: string[] = [];
  const avoidCtx = AVOID_WORDS.some((w) => low.includes(w));
  for (const brand of ALL_BRANDS) {
    const at = low.indexOf(brand.toLowerCase());
    if (at < 0) continue;
    // 要求原文里出现过回避语境词，否则不认（避免把自家品牌当竞品）
    if (!avoidCtx) continue;
    competitors.push(brand);
    push('competitor_brands', '竞品回避', 'hit', 'R-COMP-BRAND', at, brand.length, brand);
  }
  if (competitors.length === 0) {
    push('competitor_brands', '竞品回避', 'default', 'R-DEFAULT', -1, 0, '无（未识别到需要回避的品牌）');
  }

  /* ---- 受管制口径 ---- */
  let regulated: string | null = null;
  for (const [key, words] of REGULATED_WORDS) {
    for (const w of words) {
      const at = low.indexOf(w);
      if (at >= 0) {
        regulated = key;
        push('regulated_category', '受管制口径', 'hit', 'R-REG-WORD', at, w.length, key);
        break;
      }
    }
    if (regulated) break;
  }
  if (!regulated) push('regulated_category', '受管制口径', 'default', 'R-DEFAULT', -1, 0, '无（未识别）');

  /* ---- 组装 ---- */
  const nameParts = [
    categories.map((c) => CATEGORY_ZH[c] ?? c).join('+') || '不限品类',
    markets.join('/'),
    kpi,
  ];
  const spec: CampaignSpec = {
    campaign_id: options.campaignId ?? 'CUSTOM',
    name: options.name ?? `自定义 · ${nameParts.join(' · ')}`,
    target_categories: categories,
    target_markets: markets,
    target_languages: languages,
    target_age_buckets: ageBuckets,
    target_gender: gender,
    budget_usd: budget,
    kpi,
    platforms,
    competitor_brands: competitors,
    regulated_category: regulated,
    raw_text: raw,
  };

  const t1 = typeof performance !== 'undefined' ? performance.now() : Date.now();
  return { spec, evidence: ev, warnings, elapsedMs: t1 - t0 };
}

/* ------------------------------------------------------------------ */
/* 自比对：构建期 LLM 解析 vs 线上规则解析                              */
/* ------------------------------------------------------------------ */

export interface SpecDiffRow {
  field: keyof CampaignSpec;
  label: string;
  llm: string;
  rule: string;
  same: boolean;
  /** 差异说明（同项时为空） */
  note: string;
}

const DIFF_FIELDS: Array<[keyof CampaignSpec, string]> = [
  ['target_categories', '品类'],
  ['target_markets', '市场'],
  ['target_languages', '语言'],
  ['platforms', '平台'],
  ['budget_usd', '预算'],
  ['kpi', 'KPI'],
  ['target_gender', '性别'],
  ['target_age_buckets', '年龄'],
  ['competitor_brands', '竞品回避'],
  ['regulated_category', '受管制口径'],
];

function show(v: unknown): string {
  if (v === null || v === undefined || v === '') return '—';
  if (Array.isArray(v)) return v.length ? v.join('、') : '—';
  if (typeof v === 'number') return String(v);
  return String(v);
}

/** 逐字段对撞两份 spec。数组按集合比（顺序不算差异），数值按 1e-6 容差。 */
export function diffSpec(llm: CampaignSpec, rule: CampaignSpec): SpecDiffRow[] {
  return DIFF_FIELDS.map(([field, label]) => {
    const a = llm[field] as unknown;
    const b = rule[field] as unknown;
    let same: boolean;
    let note = '';
    if (Array.isArray(a) && Array.isArray(b)) {
      const sa = new Set(a.map(String));
      const sb = new Set(b.map(String));
      const missing = [...sa].filter((x) => !sb.has(x));
      const extra = [...sb].filter((x) => !sa.has(x));
      same = missing.length === 0 && extra.length === 0;
      if (!same) {
        note = [
          missing.length ? `规则版少了 ${missing.join('、')}` : '',
          extra.length ? `规则版多了 ${extra.join('、')}` : '',
        ]
          .filter(Boolean)
          .join('；');
      }
    } else if (typeof a === 'number' && typeof b === 'number') {
      same = Math.abs(a - b) < 1e-6;
      if (!same) note = `差 ${Math.abs(a - b)}`;
    } else {
      same = String(a ?? '') === String(b ?? '');
      if (!same) note = `LLM 判 ${show(a)}，规则判 ${show(b)}`;
    }
    return { field, label, llm: show(a), rule: show(b), same, note };
  });
}

/** 一条 brief 的字段级一致率（用于页面上的「差在哪」汇总）。 */
export function diffSummary(rows: SpecDiffRow[]): { total: number; same: number; rate: number } {
  const same = rows.filter((r) => r.same).length;
  return { total: rows.length, same, rate: rows.length ? same / rows.length : 1 };
}

/* ------------------------------------------------------------------ */
/* 示例 brief（降低「不知道能敲什么」的门槛）                            */
/* ------------------------------------------------------------------ */

export const EXAMPLE_BRIEFS: Array<{ label: string; text: string }> = [
  {
    label: '彩妆 · 巴西墨西哥 · 看互动',
    text: '彩妆新品投巴西和墨西哥，预算 5 万美元，Instagram 为主，目标 18-24 岁女性，看互动。',
  },
  {
    label: '扫地机 · 北美 · 看转化 · 避开 Dreame',
    text: '扫地机器人新品主打北美，预算 $120,000，TikTok 和 YouTube，目标 25-40 岁女性，看转化，避开最近合作过 Dreame 的达人。',
  },
  {
    label: '手游 · 日韩 · 看曝光 · 儿童向',
    text: '一款休闲手游在日本和韩国上线，预算 8 万美元，YouTube 为主，18-34 岁男性，先把曝光和安装打出来，品牌安全按儿童向标准审。',
  },
  {
    label: '故意写残的 brief（看降级）',
    text: '我们想做一波投放，尽快上线。',
  },
];
