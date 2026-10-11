// Validate repository-local documentation links without fetching external URLs.
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

async function markdownFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = await Promise.all(
    entries.map((entry) => {
      const target = path.join(directory, entry.name);
      if (entry.isDirectory()) return markdownFiles(target);
      return entry.name.endsWith(".md") ? [target] : [];
    }),
  );
  return files.flat();
}

function outsideFences(source) {
  let marker = null;
  return source
    .split(/\r?\n/)
    .filter((line) => {
      const fence = line.match(/^\s*(`{3,}|~{3,})/);
      if (fence) {
        if (!marker) marker = fence[1];
        else if (fence[1][0] === marker[0] && fence[1].length >= marker.length)
          marker = null;
        return false;
      }
      return marker === null;
    })
    .join("\n");
}

function headingAnchors(source) {
  const counts = new Map();
  const anchors = new Set();
  for (const match of outsideFences(source).matchAll(/^#{1,6}\s+(.+)$/gm)) {
    const base = match[1]
      .replace(/\s+#+\s*$/, "")
      .toLowerCase()
      .replace(/[^\p{L}\p{N}_\-\s]/gu, "")
      .replace(/ /g, "-");
    const count = counts.get(base) ?? 0;
    counts.set(base, count + 1);
    anchors.add(count ? `${base}-${count}` : base);
  }
  return anchors;
}

const files = [
  path.join(root, "README.md"),
  path.join(root, "CONTRIBUTING.md"),
  ...(await markdownFiles(path.join(root, "docs"))),
  ...(await markdownFiles(path.join(root, "pictures"))),
];
const errors = [];
let links = 0;

for (const file of files) {
  const source = await readFile(file, "utf8");
  const label = path.relative(root, file);
  if (/sandbox:|\uE200cite|\uE202turn/.test(source))
    errors.push(
      `${label}: contains an unavailable sandbox or citation reference`,
    );

  // Match targets, including image links nested inside another Markdown link.
  for (const match of outsideFences(source).matchAll(
    /\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g,
  )) {
    const reference = match[1];
    if (/^(?:https?:|mailto:)/i.test(reference)) continue;
    const [relative, fragment] = reference.split("#", 2);
    const target = relative
      ? path.resolve(path.dirname(file), decodeURIComponent(relative))
      : file;
    const location = path.relative(root, target);
    if (location.startsWith("..") || path.isAbsolute(location)) {
      errors.push(`${label}: link escapes repository: ${reference}`);
      continue;
    }
    try {
      const content = await readFile(target);
      links += 1;
      if (
        fragment &&
        target.endsWith(".md") &&
        !headingAnchors(content.toString("utf8")).has(
          decodeURIComponent(fragment),
        )
      ) {
        errors.push(`${label}: missing heading: ${reference}`);
      }
    } catch {
      errors.push(`${label}: missing or unreadable target: ${reference}`);
    }
  }
}

if (errors.length) {
  for (const error of errors) console.error(error);
  process.exitCode = 1;
} else {
  console.log(
    `Checked ${files.length} documents and ${links} local links/images.`,
  );
}
