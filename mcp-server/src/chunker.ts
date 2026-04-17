import { getChunkingConfig } from './db.js'

const CHARS_PER_TOKEN = 4
const { max_tokens: MAX_TOKENS, overlap_tokens: OVERLAP_TOKENS } = getChunkingConfig()

export interface Chunk {
  content: string
  lineStart: number
  lineEnd: number
  tokenCount: number
}

export function chunkText(text: string): Chunk[] {
  const lines = text.split('\n')
  const maxChars = MAX_TOKENS * CHARS_PER_TOKEN
  const overlapChars = OVERLAP_TOKENS * CHARS_PER_TOKEN

  const chunks: Chunk[] = []
  let currentLines: string[] = []
  let currentChars = 0
  let chunkStartLine = 0

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    const lineLen = line.length + 1
    currentLines.push(line)
    currentChars += lineLen

    if (currentChars >= maxChars) {
      const content = currentLines.join('\n')
      chunks.push({
        content,
        lineStart: chunkStartLine,
        lineEnd: i,
        tokenCount: Math.floor(content.length / CHARS_PER_TOKEN),
      })

      // Overlap: keep enough lines to cover overlapChars
      const overlapLines: string[] = []
      let overlapTotal = 0
      for (let j = currentLines.length - 1; j >= 0; j--) {
        overlapTotal += currentLines[j].length + 1
        overlapLines.unshift(currentLines[j])
        if (overlapTotal >= overlapChars) break
      }

      currentLines = overlapLines
      currentChars = currentLines.reduce((sum, l) => sum + l.length + 1, 0)
      chunkStartLine = i - currentLines.length + 1
    }
  }

  // Final chunk
  if (currentLines.length > 0) {
    const content = currentLines.join('\n')
    if (content.trim()) {
      chunks.push({
        content,
        lineStart: chunkStartLine,
        lineEnd: lines.length - 1,
        tokenCount: Math.floor(content.length / CHARS_PER_TOKEN),
      })
    }
  }

  return chunks
}
