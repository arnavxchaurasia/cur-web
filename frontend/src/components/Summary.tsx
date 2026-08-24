import ReactMarkdown from 'react-markdown'

// Section config: heading text → display metadata
const SECTION_META: Record<string, { icon: string; color: string; bg: string; border: string }> = {
  'where gcp wins':   { icon: '↓', color: '#22c55e', bg: 'rgba(34,197,94,0.07)',  border: 'rgba(34,197,94,0.25)' },
  'where aws wins':   { icon: '↑', color: '#f97316', bg: 'rgba(249,115,22,0.07)', border: 'rgba(249,115,22,0.25)' },
  'caveats':          { icon: '⚠', color: '#facc15', bg: 'rgba(250,204,21,0.06)', border: 'rgba(250,204,21,0.20)' },
  'confidence':       { icon: '◎', color: '#a78bfa', bg: 'rgba(167,139,250,0.07)',border: 'rgba(167,139,250,0.22)' },
}

function normKey(h: string) { return h.toLowerCase().trim() }

interface Block {
  type: 'intro' | 'section'
  heading?: string
  body: string
}

function parseBlocks(markdown: string): Block[] {
  const lines = markdown.split('\n')
  const blocks: Block[] = []
  let currentHeading: string | null = null
  let buffer: string[] = []

  const flush = () => {
    const body = buffer.join('\n').trim()
    if (!body && currentHeading === null) return
    if (currentHeading === null) {
      if (body) blocks.push({ type: 'intro', body })
    } else {
      blocks.push({ type: 'section', heading: currentHeading, body })
    }
    buffer = []
  }

  const H2_RE = /^##\s+(.+)/
  for (const line of lines) {
    const h2 = H2_RE.exec(line)
    if (h2) {
      flush()
      currentHeading = h2[1].trim()
    } else {
      buffer.push(line)
    }
  }
  flush()
  return blocks
}

function SectionBlock({ heading, body }: Readonly<{ heading: string; body: string }>) {
  const meta = SECTION_META[normKey(heading)]
  if (!meta) {
    // Unknown section — render plainly
    return (
      <div className="mt-4">
        <h4 className="text-xs font-semibold uppercase tracking-wider text-gray-400 mb-2">{heading}</h4>
        <div className="prose prose-invert prose-sm max-w-none text-gray-300 prose-li:my-0.5 prose-p:my-1.5 prose-ul:my-1.5">
          <ReactMarkdown>{body}</ReactMarkdown>
        </div>
      </div>
    )
  }

  return (
    <div
      className="rounded-lg px-4 py-3 mt-3"
      style={{ background: meta.bg, border: `1px solid ${meta.border}` }}
    >
      <div className="flex items-center gap-2 mb-2">
        <span className="text-sm font-bold" style={{ color: meta.color }}>{meta.icon}</span>
        <span className="text-xs font-semibold uppercase tracking-wider" style={{ color: meta.color }}>
          {heading}
        </span>
      </div>
      <div className="prose prose-invert prose-sm max-w-none text-gray-200
        prose-li:my-0.5 prose-p:my-1 prose-ul:my-1 prose-ol:my-1
        prose-strong:text-white">
        <ReactMarkdown>{body}</ReactMarkdown>
      </div>
    </div>
  )
}

export function Summary({ markdown }: Readonly<{ markdown: string }>) {
  const blocks = parseBlocks(markdown)

  return (
    <div className="summary-card p-5">
      <div className="flex items-center gap-2 mb-4">
        <span className="text-[#00C2BB]">◈</span>
        <h3 className="text-sm font-semibold text-[#00C2BB] uppercase tracking-wider">
          AI Summary
        </h3>
      </div>

      {blocks.map((block, i) => {
        if (block.type === 'intro') {
          return (
            <p key={`intro-${i}`} className="text-sm text-gray-300 leading-relaxed">
              {block.body}
            </p>
          )
        }
        return (
          <SectionBlock key={block.heading ?? `section-${i}`} heading={block.heading!} body={block.body} />
        )
      })}
    </div>
  )
}
