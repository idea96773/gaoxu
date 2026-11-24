// The template contents stay inert. Never mount either fragment or resolve its URLs.
const blocks = new Set('P DIV SECTION ARTICLE H1 H2 H3 H4 H5 H6 LI BLOCKQUOTE BR TR TD TH PRE FIGURE FIGCAPTION'.split(' '))
const hidden = 'script,style,template,noscript,head,svg,iframe,object,[hidden],[aria-hidden="true"]'
const compact = text => text.replace(/[\s\u200b\ufeff]+/gu, '')

function extract(html, frozen) {
  const template = document.createElement('template')
  template.innerHTML = html
  const root = template.content
  // The bridge supplies the frozen title through a separate editor field.
  if (frozen && root.firstElementChild?.tagName === 'H1') root.firstElementChild.remove()
  const chunks = [], images = [], links = [], breaks = new Set()
  let length = 0
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (node.nodeType !== Node.ELEMENT_NODE) return NodeFilter.FILTER_ACCEPT
      return node.matches(hidden) || node.style?.display === 'none' || node.style?.visibility === 'hidden'
        ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT
    }
  })
  // Record both sides of block boundaries, including text after nested paragraphs.
  const open = []
  function leave() {
    const entry = open.pop()
    if (entry.block) breaks.add(length)
    if (entry.link) entry.link.end = length
  }
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    while (open.length && open.at(-1).node !== node.parentNode) leave()
    if (node.nodeType === Node.TEXT_NODE) {
      const text = compact(node.data)
      chunks.push(text)
      length += text.length
      continue
    }
    const block = blocks.has(node.tagName)
    if (block) breaks.add(length)
    if (node.tagName === 'IMG') images.push(length)
    const link = node.tagName === 'A' ? { href: node.getAttribute('href') || '', start: length, end: length } : null
    if (link) links.push(link)
    open.push({ node, block, link })
  }
  while (open.length) leave()
  breaks.delete(0)
  breaks.delete(length)
  return { text: chunks.join(''), images, links, breaks }
}

export function compareClipboard(snapshot, html, plain) {
  if (!html && !plain.trim()) throw new Error('剪贴板中没有正文，请重新复制编辑器内的正文。')
  if ((html || plain).length > 4 * 1024 * 1024) throw new Error('复制内容过大，请核实仅选中了正文，或使用下方人工核对表。')
  const expected = extract(snapshot.html, true)
  const actual = html ? extract(html, false) : { text: compact(plain) }
  const aligned = expected.text === actual.text
  const report = { aligned, rich: Boolean(html) }
  if (!aligned) {
    let at = 0
    while (at < expected.text.length && expected.text[at] === actual.text[at]) at++
    report.difference = { context: expected.text.slice(Math.max(0, at - 16), at),
      expected: expected.text.slice(at, at + 32), actual: actual.text.slice(at, at + 32) }
  }
  if (!html) return report
  report.expectedImages = expected.images.length
  report.actualImages = actual.images.length
  if (aligned) {
    report.missingBreaks = [...expected.breaks].filter(offset => !actual.breaks.has(offset)).length
    report.extraBreaks = [...actual.breaks].filter(offset => !expected.breaks.has(offset)).length
    if (actual.images.length === expected.images.length) {
      report.movedImage = actual.images.findIndex((offset, i) => offset !== expected.images[i]) + 1
    }
  }
  if (snapshot.link.mode !== 'omit') {
    const footer = expected.links.at(-1)
    const last = actual.links.at(-1)
    const url = snapshot.link.mode === 'short' ? snapshot.link.short : snapshot.link.destination
    report.footerMatches = Boolean(footer && last && last.href === url && last.end === actual.text.length &&
      last.start < last.end && actual.text.slice(last.start) === expected.text.slice(footer.start) &&
      !actual.images.some(offset => offset > last.start))
  }
  return report
}
