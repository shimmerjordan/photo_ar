/** 与服务端 `photoar.scannability` 同一张表。服务端已经算好 `stars` 字段，这里只负责文案。 */
export const STAR_HINT = {
  1: '提不出足够特征，基本扫不出来。',
  2: '擦线过闸，真机大概率扫不出，建议换一张纹理更丰富的。',
  3: '能扫，需要对准并让照片占满取景框。',
  4: '',
  5: '',
}
export function starHint(n) { return STAR_HINT[Math.min(5, Math.max(1, n | 0))] ?? '' }
