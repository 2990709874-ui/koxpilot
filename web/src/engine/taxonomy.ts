/**
 * 共享分类学（对应 Python `src/koxpilot/taxonomy.py`）。
 *
 * 这里**只有业务分类学，没有任何判定阈值**——粉丝分层边界、品类相邻表、市场语言表
 * 属于"公开可知的业务常量"，Python 侧同样写在代码里，因此镜像到 TS 不违反
 * "阈值必须来自数据"的纪律（阈值全在 thresholds.json）。
 *
 * 漂移防护：`scripts/verify-parity.mjs` 会把 TS 算出的 `group_key` 与 verdicts.json 的
 * `group_key` 逐条比对，任何分层口径漂移都会立刻变成一致性失败，不会静默。
 */

export const BUCKET_ORDER = ['nano', 'micro', 'mid', 'macro', 'mega'] as const;
export type Bucket = (typeof BUCKET_ORDER)[number];

/** 左闭右开区间，单位：粉丝数（SPEC 3.1）。 */
export const BUCKET_BOUNDS: Record<Bucket, [number, number]> = {
  nano: [1_000, 10_000],
  micro: [10_000, 100_000],
  mid: [100_000, 500_000],
  macro: [500_000, 2_000_000],
  mega: [2_000_000, 1e12],
};

export const HEAD_BUCKETS = new Set<string>(['macro', 'mega']);
export const LONGTAIL_BUCKETS = new Set<string>(['nano', 'micro']);

/** 粉丝数 -> 分层名。缺失时归入 nano（最保守的一档，与 Python 一致）。 */
export function followerBucket(followers: number | null | undefined): string {
  if (followers === null || followers === undefined) return 'nano';
  const f = Number(followers);
  for (const name of BUCKET_ORDER) {
    const [lo, hi] = BUCKET_BOUNDS[name];
    if (lo <= f && f < hi) return name;
  }
  return f >= BUCKET_BOUNDS.mega[0] ? 'mega' : 'nano';
}

/** 分位数分组键，必须与 thresholds.json 的 key 拼接方式完全一致。 */
export function groupKey(platform: string, bucket: string): string {
  return `${platform}|${bucket}`;
}

export const PLATFORMS = ['tiktok', 'youtube', 'instagram', 'x', 'kwai'] as const;

export const PLATFORM_LABEL: Record<string, string> = {
  tiktok: 'TikTok',
  youtube: 'YouTube',
  instagram: 'Instagram',
  x: 'X',
  kwai: 'Kwai',
};

/** 国家 -> 主语言（G2.4 的市场语言集由此派生）。 */
export const COUNTRY_LANG: Record<string, string> = {
  US: 'en', CA: 'en', GB: 'en', AU: 'en', DE: 'de', FR: 'fr', ES: 'es', IT: 'it',
  NL: 'nl', SE: 'sv', PL: 'pl', JP: 'ja', KR: 'ko', TW: 'zh', HK: 'zh', SG: 'en',
  MY: 'ms', ID: 'id', VN: 'vi', TH: 'th', PH: 'en', IN: 'hi', PK: 'ur', BD: 'bn',
  BR: 'pt', MX: 'es', AR: 'es', CL: 'es', CO: 'es', PE: 'es', SA: 'ar', AE: 'ar',
  EG: 'ar', TR: 'tr', IL: 'he', ZA: 'en', NG: 'en', KE: 'en', RU: 'ru', UA: 'uk',
};

/** 目标市场 -> 可接受语言集（英语在所有市场都可接受：出海内容通行语）。 */
export function marketLanguages(market: string): Set<string> {
  const lang = COUNTRY_LANG[market];
  return lang ? new Set([lang, 'en']) : new Set<string>();
}

/** 地理邻近关系（镜像 Python `taxonomy.GEO_NEIGHBORS`）：语言与受众重叠度较高的市场。 */
export const GEO_NEIGHBORS: Record<string, string[]> = {
  US: ['CA', 'GB', 'MX'], CA: ['US', 'GB', 'FR'], GB: ['US', 'IE', 'AU'], AU: ['GB', 'US', 'NZ'],
  DE: ['AT', 'CH', 'NL'], FR: ['BE', 'CH', 'CA'], ES: ['MX', 'AR', 'CO'], IT: ['CH', 'DE', 'FR'],
  NL: ['BE', 'DE', 'GB'], SE: ['NO', 'DK', 'FI'], PL: ['DE', 'CZ', 'UA'], JP: ['TW', 'KR', 'US'],
  KR: ['JP', 'TW', 'US'], TW: ['HK', 'JP', 'SG'], HK: ['TW', 'SG', 'MY'], SG: ['MY', 'ID', 'HK'],
  MY: ['SG', 'ID', 'TH'], ID: ['MY', 'SG', 'PH'], VN: ['TH', 'ID', 'PH'], TH: ['VN', 'MY', 'ID'],
  PH: ['ID', 'SG', 'US'], IN: ['PK', 'BD', 'AE'], PK: ['IN', 'AE', 'SA'], BD: ['IN', 'PK', 'MY'],
  BR: ['PT', 'AR', 'MX'], MX: ['US', 'CO', 'AR'], AR: ['CL', 'BR', 'ES'], CL: ['AR', 'PE', 'BR'],
  CO: ['MX', 'PE', 'EC'], PE: ['CL', 'CO', 'MX'], SA: ['AE', 'EG', 'KW'], AE: ['SA', 'EG', 'IN'],
  EG: ['SA', 'AE', 'TR'], TR: ['DE', 'AE', 'RU'], IL: ['US', 'TR', 'DE'], ZA: ['NG', 'KE', 'GB'],
  NG: ['ZA', 'KE', 'GB'], KE: ['NG', 'ZA', 'GB'], RU: ['UA', 'TR', 'DE'], UA: ['PL', 'RU', 'DE'],
};

/**
 * 目标市场 → 含相邻市场的市场集合（镜像 Python `taxonomy.neighbor_markets`）。
 *
 * 三条口径与 Python 逐字相同：邻接关系只取 `GEO_NEIGHBORS`；只保留达人库覆盖的国家
 * （`COUNTRY_LANG` 的键，扩了也召不到人的国家不写进建议）；原市场保留，升序去重。
 */
export function neighborMarkets(markets: readonly string[]): string[] {
  const out = new Set<string>();
  for (const m of markets) if (COUNTRY_LANG[m]) out.add(m);
  for (const m of markets) {
    for (const n of GEO_NEIGHBORS[m] ?? []) if (COUNTRY_LANG[n]) out.add(n);
  }
  return [...out].sort();
}

export const CATEGORIES = [
  '3c_digital', 'beauty_care', 'home_appliance', 'fashion',
  'mother_baby', 'food_health', 'gaming_app', 'auto_travel',
] as const;

export const CATEGORY_ZH: Record<string, string> = {
  '3c_digital': '3C数码',
  beauty_care: '美妆个护',
  home_appliance: '家居家电',
  fashion: '服饰鞋包',
  mother_baby: '母婴',
  food_health: '食品保健',
  gaming_app: '游戏应用',
  auto_travel: '汽车出行',
};

/** 品类相邻表（G2.3 规则版打分用）。 */
export const CATEGORY_ADJACENCY: Record<string, string[]> = {
  '3c_digital': ['home_appliance', 'gaming_app'],
  beauty_care: ['fashion', 'food_health'],
  home_appliance: ['3c_digital', 'mother_baby'],
  fashion: ['beauty_care', 'auto_travel'],
  mother_baby: ['home_appliance', 'food_health'],
  food_health: ['beauty_care', 'mother_baby'],
  gaming_app: ['3c_digital', 'auto_travel'],
  auto_travel: ['gaming_app', 'fashion'],
};

/** G2.3 规则版关键词兜底（handle 命中 -> 弱证据）。 */
export const CATEGORY_KEYWORDS: Record<string, string[]> = {
  '3c_digital': ['gadget', 'tech', 'unbox', 'phone', 'laptop', 'audio', 'digital'],
  beauty_care: ['beauty', 'makeup', 'skincare', 'glow', 'cosmetic', 'kbeauty'],
  home_appliance: ['home', 'kitchen', 'appliance', 'clean', 'living', 'cook'],
  fashion: ['style', 'fashion', 'outfit', 'ootd', 'wear', 'sneaker'],
  mother_baby: ['mom', 'baby', 'kids', 'parent', 'family', 'toddler'],
  food_health: ['food', 'eat', 'recipe', 'fit', 'health', 'nutrition', 'supp'],
  gaming_app: ['game', 'gaming', 'play', 'esport', 'app', 'stream'],
  auto_travel: ['auto', 'car', 'drive', 'travel', 'trip', 'ev'],
};

export const AGE_BUCKETS = ['18-24', '25-34', '35-44', '45+'] as const;

/** 竞品组：同组内互为竞品（G3.3）。 */
export const COMPETITOR_GROUPS: string[][] = [
  ['Anker', 'Baseus', 'UGREEN', 'EarFun', 'Soundcore'],
  ['Dreame', 'Roborock', 'Narwal', 'Eureka', 'Xiaomi Home'],
  ['SHEIN', 'Cider', 'Halara', 'Urbanic', 'Zaful'],
  ['Florasis', 'Judydoll', 'Focallure', 'SHEGLAM', 'Yatsen'],
  ['Mihoyo', 'Garena', 'NetEase Games', 'TapTap', 'Playmate'],
];

export function competitorsOf(brand: string): Set<string> {
  const out = new Set<string>();
  for (const grp of COMPETITOR_GROUPS) {
    if (grp.includes(brand)) grp.forEach((b) => out.add(b));
  }
  out.delete(brand);
  return out;
}

/** 受管制品类 -> 需要额外审的内容标记类型（G3.5）。 */
export const REGULATED_CATEGORY_FLAGS: Record<string, string[]> = {
  medical: ['medical_claim', 'medical_claim_strong', 'unverified_claim'],
  finance: ['gambling_promo', 'unverified_claim'],
  kids: ['adult_content', 'profanity', 'shock_humor', 'alcohol_mention'],
};

export const HIGH_RISK_FLAG_TYPES = [
  'political_content', 'adult_content', 'gambling_promo', 'medical_claim_strong',
] as const;
