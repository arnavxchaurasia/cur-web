import { useRef, useState } from 'react'

type Props = Readonly<{ onChange: (file: File) => void; file: File | null; inputId?: string }>

export function DropZone({ onChange, file, inputId }: Props) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [dragging, setDragging] = useState(false)

  const accept = (f: File) => { onChange(f) }

  const borderClass = dragging
    ? 'dropzone-drag'
    : file
      ? 'border-[#00C2BB]/50 bg-[#00C2BB]/5 hover:border-[#00C2BB]/70'
      : 'border-white/20 hover:border-[#645DF6]/60 hover:bg-[#645DF6]/5'

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => inputRef.current?.click()}
      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') inputRef.current?.click() }}
      onDragOver={e => { e.preventDefault(); setDragging(true) }}
      onDragLeave={() => setDragging(false)}
      onDrop={e => { e.preventDefault(); setDragging(false); const f = e.dataTransfer.files[0]; if (f) accept(f) }}
      className={`border-2 border-dashed rounded-xl p-10 text-center cursor-pointer select-none transition-all duration-200 ${borderClass}`}
    >
      <input ref={inputRef} id={inputId} type="file" accept="*" className="hidden"
        onChange={e => { const f = e.target.files?.[0]; if (f) accept(f) }} />

      {file ? (
        <div className="flex flex-col items-center gap-2 anim-scale-in">
          <FileIcon />
          <p className="text-sm text-gray-200 font-medium">{file.name}</p>
          <p className="text-xs text-[#00C2BB]">Click to change file</p>
        </div>
      ) : dragging ? (
        <div className="flex flex-col items-center gap-2">
          <UploadIcon active />
          <p className="text-sm text-[#645DF6] font-medium">Drop to upload</p>
        </div>
      ) : (
        <div className="flex flex-col items-center gap-2">
          <UploadIcon active={false} />
          <p className="text-sm text-gray-400">Drop your AWS bill here</p>
          <p className="text-xs text-[#645DF6] mt-0.5">or click to browse</p>
        </div>
      )}
    </div>
  )
}

function UploadIcon({ active }: Readonly<{ active: boolean }>) {
  return (
    <svg width="32" height="32" viewBox="0 0 24 24" fill="none"
      className={`transition-colors duration-200 ${active ? 'text-[#645DF6]' : 'text-gray-600'}`}
      stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <polyline points="17 8 12 3 7 8" />
      <line x1="12" y1="3" x2="12" y2="15" />
    </svg>
  )
}

function FileIcon() {
  return (
    <svg width="28" height="28" viewBox="0 0 24 24" fill="none"
      className="text-[#00C2BB]"
      stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
    </svg>
  )
}
