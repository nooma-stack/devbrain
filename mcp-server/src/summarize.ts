import { getSummarizationConfig } from './db.js'

const { url: OLLAMA_URL, model: SUMMARIZE_MODEL } = getSummarizationConfig()

export async function summarizeSession(content: string): Promise<string> {
  const prompt = `You are a technical session summarizer. Summarize this AI coding session transcript concisely. Focus on:
- What was accomplished
- Key decisions made and why
- Files created or modified
- Issues encountered and how they were resolved
- Important patterns or lessons learned

Keep the summary under 500 words. Be specific about file names, function names, and technical details.

Transcript:
${content.slice(0, 12000)}` // Limit to ~12K chars for 7B model context

  const response = await fetch(`${OLLAMA_URL}/api/generate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      model: SUMMARIZE_MODEL,
      prompt,
      stream: false,
      options: {
        temperature: 0.3,
        num_predict: 1024,
      },
    }),
  })

  if (!response.ok) {
    throw new Error(`Ollama summarization failed: ${response.status} ${await response.text()}`)
  }

  const data = (await response.json()) as { response: string }
  return data.response.trim()
}
