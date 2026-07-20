import { VERSION, parse } from "kordoc";

function tableSummary(table) {
  const cells = (table?.cells ?? []).map((row) =>
    row.map((cell) => String(cell?.text ?? "")),
  );
  return {
    rows: Number(table?.rows ?? cells.length),
    columns: Number(
      table?.cols ?? cells.reduce((maximum, row) => Math.max(maximum, row.length), 0),
    ),
    cells,
  };
}

function blockText(block) {
  if (block?.type === "table") {
    return (block.table?.cells ?? [])
      .map((row) => row.map((cell) => String(cell?.text ?? "")).join("\t"))
      .join("\n");
  }
  return typeof block?.text === "string" ? block.text : "";
}

const inputPath = process.argv[2];
if (!inputPath) {
  throw new Error("Usage: node kordoc.mjs <document>");
}

const parsed = await parse(inputPath);
if (!parsed?.success) {
  throw new Error(parsed?.error ?? "kordoc returned success=false");
}

const blocks = Array.isArray(parsed.blocks) ? parsed.blocks : [];
const pageNumbers = blocks
  .map((block) => Number(block?.pageNumber ?? 0))
  .filter((number) => Number.isFinite(number));
const tables = blocks
  .filter((block) => block?.type === "table")
  .map((block) => tableSummary(block.table));
const imageBlocks = blocks.filter((block) => block?.type === "image");
const footnotes = blocks.filter((block) => block?.type === "footnote");
const endnotes = blocks.filter((block) => block?.type === "endnote");

console.log(
  JSON.stringify({
    schema_version: 1,
    parser: "kordoc",
    version: String(VERSION ?? "4.2.3"),
    text: blocks.map(blockText).filter(Boolean).join("\n"),
    markdown: String(parsed.markdown ?? ""),
    tables,
    images_count: Math.max(
      imageBlocks.length,
      Array.isArray(parsed.images) ? parsed.images.length : 0,
    ),
    pages_count:
      Number(parsed.metadata?.pageCount) ||
      (pageNumbers.length ? Math.max(...pageNumbers) : null),
    footnotes_count: footnotes.length,
    endnotes_count: endnotes.length,
    links_count: blocks.filter((block) => block?.type === "link").length,
    metadata: parsed.metadata ?? {},
    warnings: Array.isArray(parsed.warnings)
      ? parsed.warnings.map((warning) => String(warning))
      : [],
  }),
);
