/** 发行信息字段（专辑、日期、厂牌、曲序等）。
 *
 * 「专辑信息」一键采用只写这些字段——标题/艺人/ISRC 等音乐名称字段
 * 改动风险高，仍需逐条确认。
 */
export const NON_NAME_FIELDS = new Set([
  'ALBUM', 'DATE', 'ORIGINALDATE', 'ORIGINALYEAR',
  'MUSICBRAINZ_ALBUMID', 'MUSICBRAINZ_RELEASEGROUPID',
  'MUSICBRAINZ_RELEASETRACKID', 'MUSICBRAINZ_TRACKID',
  'MUSICBRAINZ_ALBUMARTISTID',
  'LABEL', 'CATALOGNUMBER', 'BARCODE', 'ASIN',
  'RELEASECOUNTRY', 'RELEASESTATUS', 'RELEASETYPE', 'MEDIA', 'SCRIPT',
  'TRACKNUMBER', 'DISCNUMBER', 'TOTALTRACKS', 'TRACKTOTAL',
  'TOTALDISCS', 'DISCTOTAL',
  'ALBUMARTIST', 'ALBUMARTISTS', 'ALBUMARTISTSORT',
])

/** 某条提案的「专辑信息」可写键：补空/纠正的全部 + 发行信息的冲突。 */
export function albumInfoKeys(
  rows: { key: string; action: string }[],
): string[] {
  const keys = new Set<string>()
  for (const row of rows) {
    if (row.action === 'fill' || row.action === 'correct') {
      keys.add(row.key)
    } else if (row.action === 'conflict' && NON_NAME_FIELDS.has(row.key)) {
      keys.add(row.key)
    }
  }
  return [...keys]
}
