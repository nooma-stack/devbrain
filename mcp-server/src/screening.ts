// Write-time prompt-injection screening for MCP-written rows (store,
// breadcrumb / start_session, end_session summaries).
//
// Port of the FLAT-text path of ingest/screening.py: the rule patterns, the
// invisible-character class and the look-alike map are read from the same
// ingest/screening_rules.json, and both test suites run the shared cases in
// ingest/screening_cases.json. Text an agent writes through MCP has no
// trustworthy role structure, so every segment is untrusted (as for chunks).
// Flags only: nothing is blocked; flagged rows get memory.screen_flags and are
// withheld from extraction prompts.

import { readFileSync } from 'fs'
import { resolve } from 'path'
import { parse } from 'yaml'

interface PatternSpec {
  pattern: string
  flags?: string
}

interface RulesSpec {
  text_rules: Record<string, PatternSpec[]>
  word_rules: string[]
  hidden_unicode: { tag_chars: string; bidi: string; zero_width_in_word: string; zero_width_min: number }
  invisible: string
  homoglyphs: Record<string, string>
  role_line: PatternSpec
}

const SPEC: RulesSpec = JSON.parse(
  readFileSync(resolve(import.meta.dirname, '../../ingest/screening_rules.json'), 'utf-8'),
)

function compile(p: PatternSpec, extra = ''): RegExp {
  return new RegExp(p.pattern, `${p.flags ?? ''}u${extra}`)
}

const TEXT_RULES: Array<[string, RegExp[]]> = Object.entries(SPEC.text_rules).map(
  ([rule, entries]) => [rule, entries.map((e) => compile(e))],
)
const WORD_RULES = new Set(SPEC.word_rules)
const TAG_CHARS = compile({ pattern: SPEC.hidden_unicode.tag_chars })
const BIDI = compile({ pattern: SPEC.hidden_unicode.bidi })
const ZERO_WIDTH = compile({ pattern: SPEC.hidden_unicode.zero_width_in_word }, 'g')
const ZERO_WIDTH_MIN = SPEC.hidden_unicode.zero_width_min
const INVISIBLE = compile({ pattern: SPEC.invisible }, 'g')
const HOMOGLYPHS = new Map(Object.entries(SPEC.homoglyphs))
const ROLE_LINE = compile(SPEC.role_line, 'g')

// -- normalization (mirrors screening.normalize / squash) --------------------

const NAMED_ENTITIES: Record<string, string> = {
  amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: '\u00a0',
  sol: '/', bsol: '\\', vert: '|', verbar: '|', colon: ':', lowbar: '_', hyphen: '-',
}

function unescapeHtml(s: string): string {
  return s.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);?/giu, (m, body: string) => {
    if (body[0] === '#') {
      const code = body[1] === 'x' || body[1] === 'X' ? parseInt(body.slice(2), 16) : parseInt(body.slice(1), 10)
      return Number.isFinite(code) && code > 0 && code <= 0x10ffff ? String.fromCodePoint(code) : m
    }
    const named = NAMED_ENTITIES[body.toLowerCase()]
    return named ?? m
  })
}

function unescapeAll(s: string): string {
  s = unescapeHtml(s)
  s = s.replace(/\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})/gu, (_m, u?: string, x?: string) =>
    String.fromCharCode(parseInt((u ?? x) as string, 16)),
  )
  if (s.includes('%')) {
    s = s.replace(/%([0-9a-fA-F]{2})/gu, (_m, h: string) => String.fromCharCode(parseInt(h, 16)))
  }
  return s
}

export function normalize(text: string): string {
  let s = unescapeAll(text).normalize('NFKC').replace(INVISIBLE, '')
  let out = ''
  for (const ch of s) out += HOMOGLYPHS.get(ch) ?? ch
  s = out
  return s.toLowerCase()
}

export function squash(normalized: string): string {
  const s = normalized.replace(/[^0-9a-z]+/gu, ' ')
  return s.replace(/\b(?:[a-z0-9] ){3,}[a-z0-9]\b/gu, (m) => m.replace(/ /g, ''))
}

// -- screening ---------------------------------------------------------------

function segments(text: string): Array<[number, number]> {
  const starts: number[] = []
  ROLE_LINE.lastIndex = 0
  for (const m of text.matchAll(ROLE_LINE)) starts.push(m.index ?? 0)
  if (starts.length === 0) return text ? [[0, text.length]] : []
  const out: Array<[number, number]> = []
  if (starts[0] > 0) out.push([0, starts[0]])
  starts.forEach((s, i) => out.push([s, i + 1 < starts.length ? starts[i + 1] : text.length]))
  return out
}

function hiddenUnicode(s: string): boolean {
  if (TAG_CHARS.test(s) || BIDI.test(s)) return true
  return (s.match(ZERO_WIDTH)?.length ?? 0) >= ZERO_WIDTH_MIN
}

function firstMatch(patterns: RegExp[], s: string): boolean {
  return patterns.some((p) => p.test(s))
}

/** Sorted rule ids for flat text (every segment untrusted). */
export function screenText(text: string | null | undefined): string[] {
  if (!text) return []
  const flags = new Set<string>()
  for (const [start, end] of segments(text)) {
    const body = text.slice(start, end)
    if (hiddenUnicode(body)) flags.add('hidden_unicode')
    let norm: string | null = null
    let squashed: string | null = null
    for (const [rule, patterns] of TEXT_RULES) {
      if (firstMatch(patterns, body)) {
        flags.add(rule)
        continue
      }
      norm ??= normalize(body)
      let hit = firstMatch(patterns, norm)
      if (!hit && WORD_RULES.has(rule)) {
        squashed ??= squash(norm)
        hit = firstMatch(patterns, squashed)
      }
      if (hit) {
        flags.add(rule)
        flags.add('obfuscated')
      }
    }
  }
  return [...flags].sort()
}

let cachedYamlEnabled: boolean | null = null

/** env DEVBRAIN_SCREENING > yaml ingest.screening.mode > on (as ingest). */
export function screeningEnabled(): boolean {
  const off = (v: unknown) => v === false || /^(off|0|false|no)$/i.test(String(v ?? '').trim())
  const env = process.env.DEVBRAIN_SCREENING
  if (env !== undefined) return !off(env)
  if (cachedYamlEnabled === null) {
    try {
      const configPath =
        process.env.DEVBRAIN_CONFIG || resolve(import.meta.dirname, '../../config/devbrain.yaml')
      const cfg = parse(readFileSync(configPath, 'utf-8')) ?? {}
      const mode = cfg?.ingest?.screening?.mode
      cachedYamlEnabled = mode === undefined || mode === null ? true : !off(mode)
    } catch {
      cachedYamlEnabled = true
    }
  }
  return cachedYamlEnabled
}

/** Flags to store: null when clean or screening is off (memory.screen_flags stays NULL). */
export function screenFlags(text: string | null | undefined): string[] | null {
  if (!screeningEnabled()) return null
  const flags = screenText(text)
  return flags.length ? flags : null
}
