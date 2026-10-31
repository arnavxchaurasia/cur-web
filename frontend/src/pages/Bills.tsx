import { useEffect, useState, useMemo } from 'react'
import { Layout } from '../components/Layout'
import { listUploadedBills, previewUploadedBill, type UploadedBill, type BillPreviewResponse } from '../api/admin'
import type { UserInfo } from '../api/auth'
import { useTitle } from '../lib/useTitle'
import { BackButton } from '../components/BackButton'

function fmtBytes(bytes: number): string {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i]
}

export function Bills({ user }: Readonly<{ user: UserInfo }>) {
  useTitle('Uploaded Bills — admin')
  const [bills, setBills] = useState<UploadedBill[]>([])
  const [error, setError] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(false)

  // Preview modal state
  const [previewModalOpen, setPreviewModalOpen] = useState(false)
  const [previewData, setPreviewData] = useState<BillPreviewResponse | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [selectedBill, setSelectedBill] = useState<UploadedBill | null>(null)

  const isAdmin = Boolean(user?.is_admin)

  const refreshBills = async () => {
    setError(null)
    setLoading(true)
    try {
      const res = await listUploadedBills()
      setBills(res.bills || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'failed to load bills')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refreshBills()
  }, [])

  const handleOpenPreview = async (bill: UploadedBill) => {
    setSelectedBill(bill)
    setPreviewModalOpen(true)
    setPreviewLoading(true)
    setPreviewError(null)
    setPreviewData(null)

    // For non-PDF files, fetch structured lines preview
    const isPdf = bill.input_ext?.toLowerCase() === '.pdf' || bill.file_name?.toLowerCase().endsWith('.pdf')
    if (isPdf) {
      setPreviewLoading(false)
      return
    }

    try {
      const data = await previewUploadedBill(bill.content_hash)
      setPreviewData(data)
    } catch (err) {
      setPreviewError(err instanceof Error ? err.message : 'Failed to preview file contents')
    } finally {
      setPreviewLoading(false)
    }
  }

  const filteredBills = useMemo(() => {
    if (!search.trim()) return bills
    const q = search.toLowerCase().trim()
    return bills.filter(
      b =>
        b.file_name?.toLowerCase().includes(q) ||
        b.content_hash?.toLowerCase().includes(q) ||
        b.input_ext?.toLowerCase().includes(q)
    )
  }, [bills, search])

  return (
    <Layout user={user}>
      <div className="max-w-6xl mx-auto px-6 py-8">
        <BackButton fallback="/admin" />
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 mb-8">
          <div>
            <h1 className="text-2xl font-bold tracking-tight text-white">Uploaded Bills</h1>
            <p className="text-sm text-gray-400 mt-1">
              All AWS bill files stored in the database. Download original bills or view their contents and schemas.
            </p>
          </div>
        </div>

        {error && (
          <div className="mb-6 p-3 rounded-lg bg-orange-950/40 border border-orange-500/40 text-orange-400 text-xs">
            {error}
          </div>
        )}

        {/* Filter and stats toolbar */}
        <div className="flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-3 mb-4">
          <div className="relative flex-1 max-w-md">
            <span className="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none text-gray-500">
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"
                />
              </svg>
            </span>
            <input
              type="text"
              placeholder="Search by filename or content hash..."
              value={search}
              onChange={e => setSearch(e.target.value)}
              className="w-full pl-9 pr-4 py-2 bg-white/[0.03] border border-white/10 rounded-lg text-sm text-gray-200 placeholder-gray-500 focus:outline-none focus:border-[#00C2BB]/60 focus:ring-1 focus:ring-[#00C2BB]/60 transition"
            />
          </div>

          <button
            onClick={refreshBills}
            disabled={loading}
            className="inline-flex items-center justify-center gap-1.5 px-3 py-2 bg-white/5 hover:bg-white/10 border border-white/10 rounded-lg text-xs font-medium text-gray-300 transition"
          >
            <svg
              className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`}
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"
              />
            </svg>
            Refresh List
          </button>
        </div>

        {/* Bills Table */}
        <div className="border border-white/10 rounded-lg overflow-hidden bg-black/30">
          <div className="bg-white/[0.02] border-b border-white/10 px-5 py-4 flex items-center justify-between">
            <div>
              <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wider">Database Bills</h2>
              <p className="text-xs text-gray-500 mt-0.5">
                {filteredBills.length} of {bills.length} bill{bills.length !== 1 ? 's' : ''} shown
              </p>
            </div>
            <span className="text-[11px] font-mono text-gray-400 bg-white/5 px-2.5 py-1 rounded border border-white/10">
              Total Storage: {fmtBytes(bills.reduce((acc, b) => acc + (b.file_size || 0), 0))}
            </span>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse">
              <thead>
                <tr className="border-b border-white/10 bg-white/[0.02]">
                  <th className="pb-3 pt-4 text-gray-400 text-xs px-5">File Name</th>
                  <th className="pb-3 pt-4 text-gray-400 text-xs px-5">Type</th>
                  <th className="pb-3 pt-4 text-gray-400 text-xs px-5">Size</th>
                  <th className="pb-3 pt-4 text-gray-400 text-xs px-5">Uploaded At</th>
                  <th className="pb-3 pt-4 text-gray-400 text-xs px-5 font-mono">Content Hash</th>
                  <th className="pb-3 pt-4 text-gray-400 text-xs px-5 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-white/5">
                {filteredBills.length === 0 ? (
                  <tr>
                    <td className="px-5 py-8 text-center text-gray-500 text-xs" colSpan={6}>
                      {bills.length === 0 ? 'No bills uploaded yet.' : 'No bills matching your search filter.'}
                    </td>
                  </tr>
                ) : (
                  filteredBills.map(b => (
                    <tr key={b.content_hash} className="hover:bg-white/[0.02] transition-colors group">
                      <td className="px-5 py-4 text-gray-200 font-medium flex items-center gap-2">
                        <svg
                          className="w-4 h-4 text-[#00C2BB] shrink-0"
                          fill="none"
                          viewBox="0 0 24 24"
                          stroke="currentColor"
                        >
                          <path
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            strokeWidth={1.8}
                            d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
                          />
                        </svg>
                        <span className="truncate max-w-[280px]" title={b.file_name}>
                          {b.file_name || 'unnamed_bill'}
                        </span>
                      </td>
                      <td className="px-5 py-4 text-gray-400 text-xs">
                        <span className="px-2 py-0.5 rounded bg-white/5 border border-white/10 uppercase text-[10px] font-mono tracking-wide">
                          {b.input_ext?.replace('.', '') || 'UNKNOWN'}
                        </span>
                      </td>
                      <td className="px-5 py-4 text-gray-400 text-xs whitespace-nowrap">{fmtBytes(b.file_size)}</td>
                      <td className="px-5 py-4 text-gray-400 text-xs whitespace-nowrap">
                        {new Date(b.created_at).toLocaleString()}
                      </td>
                      <td
                        className="px-5 py-4 text-gray-400 text-xs font-mono truncate max-w-[150px]"
                        title={b.content_hash}
                      >
                        {b.content_hash.slice(0, 12)}…{b.content_hash.slice(-4)}
                      </td>
                      <td className="px-5 py-4 text-right whitespace-nowrap">
                        <div className="flex items-center justify-end gap-2">
                          {/* View Button */}
                          <button
                            onClick={() => handleOpenPreview(b)}
                            className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md bg-white/5 hover:bg-[#00C2BB]/20 hover:text-[#00C2BB] text-gray-300 text-xs font-medium border border-white/10 hover:border-[#00C2BB]/40 transition"
                            title="View / inspect bill"
                          >
                            <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                              <path
                                strokeLinecap="round"
                                strokeLinejoin="round"
                                strokeWidth={2}
                                d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"
                              />
                              <path
                                strokeLinecap="round"
                                strokeLinejoin="round"
                                strokeWidth={2}
                                d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"
                              />
                            </svg>
                            View
                          </button>

                          {/* Direct Download Button */}
                          <a
                            href={`/api/admin/bills/${encodeURIComponent(b.content_hash)}/download`}
                            download={b.file_name || undefined}
                            className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md bg-[#00C2BB]/10 hover:bg-[#00C2BB]/20 text-[#00C2BB] text-xs font-medium border border-[#00C2BB]/30 hover:border-[#00C2BB]/60 transition"
                            title="Download original file"
                          >
                            <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                              <path
                                strokeLinecap="round"
                                strokeLinejoin="round"
                                strokeWidth={2}
                                d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"
                              />
                            </svg>
                            Download
                          </a>
                        </div>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>

        {!isAdmin && (
          <div className="mt-6 p-4 rounded-lg bg-amber-950/20 border border-amber-500/30 text-amber-400 text-xs">
            <p className="font-medium">Admin access required</p>
            <p className="text-[10px] text-amber-400/70 mt-1">
              Only administrators can view and download database bills and manage notification settings.
            </p>
          </div>
        )}
      </div>

      {/* Bill Preview Modal */}
      {previewModalOpen && selectedBill && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-sm animate-in fade-in duration-150">
          <div className="relative w-full max-w-5xl max-h-[90vh] bg-[#0c0d14] border border-white/20 rounded-2xl shadow-2xl flex flex-col overflow-hidden">
            {/* Modal Header */}
            <div className="px-6 py-4 border-b border-white/10 flex items-center justify-between bg-white/[0.02]">
              <div className="flex items-center gap-3">
                <div className="w-8 h-8 rounded-lg bg-[#00C2BB]/10 border border-[#00C2BB]/30 flex items-center justify-center text-[#00C2BB]">
                  <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth={2}
                      d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
                    />
                  </svg>
                </div>
                <div>
                  <h3 className="text-base font-semibold text-white truncate max-w-lg">
                    {selectedBill.file_name}
                  </h3>
                  <div className="flex items-center gap-3 text-xs text-gray-400 mt-0.5">
                    <span>Size: {fmtBytes(selectedBill.file_size)}</span>
                    <span>•</span>
                    <span className="font-mono text-[11px]">{selectedBill.content_hash.slice(0, 20)}…</span>
                  </div>
                </div>
              </div>

              <div className="flex items-center gap-2">
                <a
                  href={`/api/admin/bills/${encodeURIComponent(selectedBill.content_hash)}/view`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="px-3 py-1.5 rounded-lg bg-white/5 hover:bg-white/10 text-gray-300 text-xs font-medium border border-white/10 inline-flex items-center gap-1.5 transition"
                >
                  <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth={2}
                      d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"
                    />
                  </svg>
                  Open Raw
                </a>
                <a
                  href={`/api/admin/bills/${encodeURIComponent(selectedBill.content_hash)}/download`}
                  download={selectedBill.file_name || undefined}
                  className="px-3 py-1.5 rounded-lg bg-[#00C2BB] hover:bg-[#00C2BB]/90 text-black text-xs font-semibold inline-flex items-center gap-1.5 transition"
                >
                  <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth={2}
                      d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"
                    />
                  </svg>
                  Download File
                </a>
                <button
                  onClick={() => setPreviewModalOpen(false)}
                  className="p-1.5 rounded-lg hover:bg-white/10 text-gray-400 hover:text-white transition ml-2"
                >
                  <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
            </div>

            {/* Modal Content */}
            <div className="flex-1 overflow-y-auto p-6 bg-black/40">
              {previewLoading ? (
                <div className="flex flex-col items-center justify-center py-20 text-gray-400 space-y-3">
                  <svg className="w-8 h-8 animate-spin text-[#00C2BB]" fill="none" viewBox="0 0 24 24">
                    <circle
                      className="opacity-25"
                      cx="12"
                      cy="12"
                      r="10"
                      stroke="currentColor"
                      strokeWidth="4"
                    />
                    <path
                      className="opacity-75"
                      fill="currentColor"
                      d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
                    />
                  </svg>
                  <p className="text-sm">Reading bill sample lines...</p>
                </div>
              ) : previewError ? (
                <div className="p-4 rounded-xl bg-orange-950/30 border border-orange-500/30 text-orange-300 text-sm">
                  <p className="font-semibold mb-1">Unable to preview bill content</p>
                  <p className="text-xs text-orange-400/80">{previewError}</p>
                </div>
              ) : selectedBill.input_ext?.toLowerCase() === '.pdf' ||
                selectedBill.file_name?.toLowerCase().endsWith('.pdf') ? (
                <div className="h-[65vh] w-full rounded-lg overflow-hidden border border-white/10">
                  <iframe
                    src={`/api/admin/bills/${encodeURIComponent(selectedBill.content_hash)}/view`}
                    className="w-full h-full bg-white"
                    title="PDF Viewer"
                  />
                </div>
              ) : previewData?.lines && previewData.lines.length > 0 ? (
                <div className="space-y-3">
                  <div className="flex items-center justify-between text-xs text-gray-400 px-1">
                    <span>
                      Showing first {previewData.lines.length} lines of file
                      {previewData.is_truncated && ' (truncated for quick inspection)'}
                    </span>
                    <span className="font-mono text-gray-500">{selectedBill.input_ext}</span>
                  </div>

                  <div className="rounded-xl border border-white/10 bg-[#07080c] p-4 overflow-x-auto font-mono text-xs text-gray-300 leading-relaxed max-h-[60vh] select-text">
                    <table className="w-full border-collapse">
                      <tbody>
                        {previewData.lines.map((line, idx) => (
                          <tr
                            key={idx}
                            className={`hover:bg-white/5 transition-colors ${
                              idx === 0 ? 'text-[#00C2BB] font-semibold bg-white/[0.02]' : ''
                            }`}
                          >
                            <td className="pr-4 py-1 text-gray-600 select-none text-right font-mono text-[10px] w-12 border-r border-white/5">
                              {idx + 1}
                            </td>
                            <td className="pl-4 py-1 whitespace-pre break-all">{line}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              ) : (
                <div className="text-center py-16 text-gray-500 text-sm">
                  No preview text available. You can download the file directly via the button above.
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </Layout>
  )
}

