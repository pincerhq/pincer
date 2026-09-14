import { getBaseUrl, getToken } from "@/api/client"

/**
 * Trigger a browser download of an authenticated API response.
 *
 * A plain `<a href>` cannot carry the bearer token, so the bytes are fetched
 * first and handed to the browser as a blob. The filename comes from the
 * server's Content-Disposition — the server knows whether the export hit its
 * row cap and says so in the name, which a client-side guess would lose.
 */
export async function downloadFromApi(path: string, fallbackName: string): Promise<{ rows: number; truncated: boolean }> {
  const token = getToken()
  const response = await fetch(`${getBaseUrl()}/${path.replace(/^\//, "")}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  if (!response.ok) {
    throw new Error(`Export failed (${response.status})`)
  }

  const disposition = response.headers.get("Content-Disposition") ?? ""
  const match = /filename="?([^"]+)"?/.exec(disposition)
  const blob = await response.blob()
  saveBlob(blob, match?.[1] ?? fallbackName)

  return {
    rows: Number(response.headers.get("X-Export-Rows") ?? 0),
    truncated: response.headers.get("X-Export-Truncated") === "true",
  }
}

/** Hand a blob to the browser as a file. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement("a")
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  // Revoked on the next tick: revoking synchronously races the download in
  // Safari, which reads the URL after the click handler returns.
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

/**
 * CSV for data that is already in the browser (a chart's own series).
 *
 * Values are quoted whenever they could otherwise break the row, and a leading
 * `=`, `+`, `-` or `@` is prefixed with an apostrophe so a spreadsheet treats
 * it as text rather than as a formula.
 */
export function toCsv(columns: string[], rows: Array<Record<string, unknown>>): string {
  const escape = (value: unknown): string => {
    if (value === null || value === undefined) return ""
    let text = String(value)
    if (/^[=+\-@]/.test(text)) text = `'${text}`
    return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
  }
  const lines = [columns.join(",")]
  for (const row of rows) {
    lines.push(columns.map((column) => escape(row[column])).join(","))
  }
  return lines.join("\n")
}

export function downloadCsv(filename: string, columns: string[], rows: Array<Record<string, unknown>>): void {
  saveBlob(new Blob([toCsv(columns, rows)], { type: "text/csv;charset=utf-8" }), filename)
}

export function downloadJson(filename: string, data: unknown): void {
  saveBlob(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }), filename)
}
