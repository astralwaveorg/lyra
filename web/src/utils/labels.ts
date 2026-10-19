/** 枚举值 → 中文标签。**界面上不允许直接显示英文枚举。** */

export const LYRIC_STATE: Record<string, string> = {
  synced: '合法歌词',
  synced_suspicious: '时间轴异常',
  plain_text: '无时间轴',
  absent: '无歌词',
  unparseable: '无法解析',
}

export const LYRIC_STATE_TONE: Record<string, 'ok' | 'warn' | 'danger' | 'info' | 'muted'> = {
  synced: 'ok',
  synced_suspicious: 'warn',
  plain_text: 'warn',
  absent: 'muted',
  unparseable: 'danger',
}

export const ISSUE_LABEL: Record<string, string> = {
  empty_sidecar: '歌词为空',
  orphan_sidecar: '歌词无归属',
  duplicate_sidecar: '歌词重复',
  sidecar_name_mismatch: '歌词名不符',
  sidecar_ambiguous: '歌词归属不明',
  sidecar_bom: '歌词含 BOM',
  sidecar_unparseable: '歌词无法解析',
  container_mismatch: '扩展名不符',
  unsupported_format: '格式不支持',
  unparseable_audio: '音频无法解析',
  no_embedded_cover: '缺内嵌封面',
  no_cover_artwork: '缺封面图',
  missing_mbid: '缺 MBID',
  missing_isrc: '缺 ISRC',
  missing_title: '缺标题',
  missing_artist: '缺艺术家',
  missing_album: '缺专辑',
  missing_genre: '缺流派',
  missing_sidecar: '缺 .lrc',
  missing_embedded: '缺内嵌歌词',
  no_lyrics: '无歌词',
  plain_embedded_lyrics: '内嵌无时间轴',
  suspicious_lyrics: '时间轴异常',
  lyrics_mismatch: '歌词不符',
}

export function issueLabel(code: string): string {
  return ISSUE_LABEL[code] ?? code
}

/** 问题 → 补齐它需要的标签字段。空数组表示换一种处理方式（清理、改名等）。 */
export const ISSUE_FIELDS: Record<string, string[]> = {
  missing_mbid: ['MUSICBRAINZ_TRACKID', 'MUSICBRAINZ_ALBUMID', 'MUSICBRAINZ_RELEASETRACKID'],
  missing_isrc: ['ISRC'],
  missing_title: ['TITLE'],
  missing_artist: ['ARTIST', 'ARTISTS'],
  missing_album: ['ALBUM', 'ALBUMARTIST', 'DATE'],
  missing_genre: ['GENRE'],
  missing_sidecar: ['TITLE', 'ARTIST'],
  missing_embedded: ['TITLE', 'ARTIST'],
  no_lyrics: ['TITLE', 'ARTIST'],
  plain_embedded_lyrics: ['TITLE', 'ARTIST'],
  suspicious_lyrics: ['TITLE', 'ARTIST'],
  lyrics_mismatch: ['TITLE', 'ARTIST'],
  no_embedded_cover: ['ALBUM'],
  no_cover_artwork: ['ALBUM'],
}

/** 问题分类：决定点开时优先给哪个动作。 */
export type FixRoute = 'lyrics' | 'tags' | 'cover' | 'rename' | 'clean'

export const ISSUE_ROUTE: Record<string, FixRoute> = {
  missing_mbid: 'tags',
  missing_isrc: 'tags',
  missing_title: 'tags',
  missing_artist: 'tags',
  missing_album: 'tags',
  missing_genre: 'tags',
  no_embedded_cover: 'cover',
  no_cover_artwork: 'cover',
  missing_sidecar: 'lyrics',
  missing_embedded: 'lyrics',
  no_lyrics: 'lyrics',
  plain_embedded_lyrics: 'lyrics',
  suspicious_lyrics: 'lyrics',
  lyrics_mismatch: 'lyrics',
  sidecar_name_mismatch: 'rename',
  sidecar_ambiguous: 'rename',
  empty_sidecar: 'clean',
  orphan_sidecar: 'clean',
  duplicate_sidecar: 'clean',
  sidecar_bom: 'clean',
  sidecar_unparseable: 'clean',
}

export const PROVIDER_LABEL: Record<string, string> = {
  qq: 'QQ 音乐',
  netease: '网易云音乐',
  lrclib: 'LRCLIB',
  acoustid: 'AcoustID',
  musicbrainz: 'MusicBrainz',
}

export function providerLabel(name: string): string {
  return PROVIDER_LABEL[name] ?? name
}

export const ORIGIN_LABEL: Record<string, string> = {
  auto: '自动匹配',
  manual: '人工选定',
  review: '复核采用',
  aligned: '对齐生成',
  generated: 'AI 生成',
}

export function originLabel(origin: string | null | undefined): string {
  return ORIGIN_LABEL[origin ?? ''] ?? '自动匹配'
}

export const FILE_KIND_LABEL: Record<string, string> = {
  audio: '音频',
  lyrics: '歌词',
  image: '图片',
  text: '文本',
}

export const TASK_KIND_LABEL: Record<string, string> = {
  fetch: '抓取歌词',
  apply: '写入歌词',
  'tags-audit': '标签审计',
  'tags-scan': '标签扫描',
}

export const TASK_STATE_LABEL: Record<string, string> = {
  running: '进行中',
  done: '已完成',
  failed: '失败',
  // 进程重启时被标记的状态；不映射就会在界面上直接显示 interrupted
  interrupted: '已中断',
}

export const TXN_STATE_LABEL: Record<string, string> = {
  prepared: '已准备',
  backup_done: '已备份',
  staged: '已暂存',
  audio_committed: '音频已写入',
  sidecar_committed: '已写入',
  completed: '已完成',
  recovery_conflict: '恢复冲突',
  rolled_back: '已回滚',
}

export function txnStateLabel(state: string): string {
  return TXN_STATE_LABEL[state] ?? state
}

/** 判定结论。后端 decision 是 accept / review / reject。 */
export const DECISION_LABEL: Record<string, string> = {
  accept: '可自动采用',
  review: '需要人工确认',
  reject: '不建议采用',
}

export const ORPHAN_ACTION_LABEL: Record<string, string> = {
  duplicate: '与已有歌词重复',
  choose: '需指定归属',
  unmatched: '没有对应音频',
}

/** 音源覆盖标签的展示顺序。 */
export const TAG_FIELD_LABEL: Record<string, string> = {
  TITLE: '标题',
  ARTIST: '艺术家',
  ARTISTS: '艺术家（多值）',
  ARTISTSORT: '艺术家排序',
  ALBUM: '专辑',
  ALBUMARTIST: '专辑艺术家',
  ALBUMARTISTS: '专辑艺术家（多值）',
  ALBUMARTISTSORT: '专辑艺术家排序',
  GENRE: '流派',
  DATE: '发行日期',
  ORIGINALDATE: '最早发行日期',
  ORIGINALYEAR: '最早发行年份',
  TRACKNUMBER: '音轨号',
  DISCNUMBER: '碟号',
  TOTALTRACKS: '音轨总数',
  TRACKTOTAL: '音轨总数',
  TOTALDISCS: '碟总数',
  DISCTOTAL: '碟总数',
  LABEL: '厂牌',
  CATALOGNUMBER: '目录号',
  BARCODE: '条码',
  ASIN: 'ASIN',
  ISRC: 'ISRC',
  COMPOSER: '作曲',
  LYRICIST: '作词',
  ARRANGER: '编曲',
  PRODUCER: '制作人',
  ENGINEER: '工程师',
  MIXER: '混音',
  CONDUCTOR: '指挥',
  REMIXER: '重混',
  RELEASECOUNTRY: '发行地区',
  RELEASESTATUS: '发行状态',
  RELEASETYPE: '发行类型',
  MEDIA: '介质',
  SCRIPT: '文字',
  LANGUAGE: '语言',
  MUSICBRAINZ_TRACKID: '录音 MBID',
  MUSICBRAINZ_ALBUMID: '专辑 MBID',
  MUSICBRAINZ_RELEASETRACKID: '发行曲目 MBID',
  MUSICBRAINZ_ARTISTID: '艺人 MBID',
  MUSICBRAINZ_ALBUMARTISTID: '专辑艺人 MBID',
  MUSICBRAINZ_RELEASEGROUPID: '发行组 MBID',
}

export function tagFieldLabel(key: string): string {
  return TAG_FIELD_LABEL[key] ?? key
}

export function stateTone(state: string | null | undefined): 'ok' | 'warn' | 'danger' | 'info' | 'muted' {
  return LYRIC_STATE_TONE[state ?? ''] ?? 'muted'
}
