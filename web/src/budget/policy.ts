/**
 * 预算分配的约束与价值模型常量（对应 Python `budget/policy.py`，SPEC 第 5 节）。
 *
 * 纪律与 Python 侧完全一致：**这是 budget/ 下唯一允许出现数字的文件**，
 * 每个数字都注明"出自 SPEC 原文"还是"本实现的显式设计假设"。
 * allocator/value 里不得出现裸常量（0/1 这类结构性数字除外）。
 */

/** 单达人预算占比上限（SPEC 5）。 */
export const SINGLE_KOX_MAX_SHARE = 0.2;
/** 头部（macro + mega）金额占比上限（SPEC 5）。 */
export const HEAD_MAX_SHARE = 0.45;
/** 长尾（nano + micro）金额占比下限（SPEC 5）。 */
export const LONGTAIL_MIN_SHARE = 0.25;
/** 单一国家金额占比上限（SPEC 5）。 */
export const COUNTRY_MAX_SHARE = 0.6;

/**
 * kpi_weight = er_ratio^a × comment_ratio^b（比值均为同组归一后的值）。
 * 指数是显式设计选择，可口述：要量不给互动加成；要转化则评论与互动同级。
 */
export const KPI_EXPONENTS: Record<string, [number, number]> = {
  reach: [0.0, 0.0],
  engagement: [1.0, 0.0],
  conversion: [0.5, 0.5],
  balanced: [0.5, 0.25],
};

/** 归一比值的截断区间：极端号（互动率 30 倍于同组中位数）本身多为异常形态。 */
export const RATIO_CLAMP: [number, number] = [0.25, 3.0];

/** 报价缺失时的估算口径（SPEC 5：按同组 avg_cpm 估算）。取 P50 而非均值，CPM 右偏。 */
export const PRICE_ESTIMATE_QUANTILE = 'p50';

/** 期望利用率下限，仅用于报告"是否明显花不完"，不是硬约束。 */
export const MIN_UTILIZATION_TARGET = 0.9;

/** 防御性迭代上限，正常远不会触达。 */
export const MAX_REPAIR_STEPS = 4000;
/** 结构修正最大轮数：多条约束互相打架，必须迭代到不动点。 */
export const MAX_REPAIR_ROUNDS = 8;

/** review 档默认不进分配池：review 的语义是"待人核"，自动分配不该替人拍板。 */
export const INCLUDE_REVIEW_BY_DEFAULT = false;

/** 采购单位是"内容条数"而不是"人"：一个达人一个 campaign 内最多投 3 条。 */
export const MAX_POSTS_PER_KOX = 3;
/** 同一达人第 n 条的边际曝光衰减（受众重叠 → 净增触达递减）。显式建模假设。 */
export const POST_MARGINAL_DECAY = 0.7;
/** 敏感性对照档位：证明"选谁"的结论不依赖这个假设。 */
export const POST_DECAY_SCAN: readonly number[] = [0.5, 0.7, 0.9];

/** n 条内容的等效条数（几何衰减求和），用它算曝光避免把重复触达当新增触达。 */
export function effectivePosts(posts: number, decay: number = POST_MARGINAL_DECAY): number {
  let total = 0.0;
  for (let i = 0; i < Math.max(0, posts); i += 1) total += decay ** i;
  return total;
}

/** 落盘/展示用的约束快照（前端直接引用，避免两处写死同一批数字）。 */
export function constraintSnapshot(): Record<string, number> {
  return {
    single_kox_max_share: SINGLE_KOX_MAX_SHARE,
    head_max_share: HEAD_MAX_SHARE,
    longtail_min_share: LONGTAIL_MIN_SHARE,
    country_max_share: COUNTRY_MAX_SHARE,
    min_utilization_target: MIN_UTILIZATION_TARGET,
    max_posts_per_kox: MAX_POSTS_PER_KOX,
    post_marginal_decay: POST_MARGINAL_DECAY,
  };
}
