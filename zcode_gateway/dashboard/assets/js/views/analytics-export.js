// Exports are generated in the browser: account data never goes to a service.

export function csvText(columns, rows) {
  const cell = (value) => {
    let text = String(value ?? "");
    // An account name or key label is untrusted spreadsheet input. Quoting
    // alone does not prevent formula execution when the CSV is opened.
    if (/^[\s]*[=+\-@]/.test(text) || /^[\t\r\n]/.test(text)) text = "'" + text;
    return '"' + text.replace(/"/g, '""') + '"';
  };
  return [columns, ...rows].map(row => row.map(cell).join(",")).join("\r\n") + "\r\n";
}

export function downloadCsv(filename, columns, rows) {
  const blob = new Blob(["\uFEFF", csvText(columns, rows)], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function comparison(current, previous, unit = "%") {
  if (previous == null) return "No previous-period comparison";
  if (unit === "pp") {
    const diff = current - previous;
    return (diff > 0 ? "+" : "") + diff.toFixed(1) + " percentage points vs previous period";
  }
  if (!previous) return current ? "New activity; previous period had none" : "No change from previous period";
  const diff = ((current - previous) / previous) * 100;
  return (diff > 0 ? "+" : "") + diff.toFixed(1) + "% vs previous period";
}
