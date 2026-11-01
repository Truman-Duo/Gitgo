import React, {useContext, useMemo, useRef} from "react";
import {Ansi, Box, Text as InkText, type TextProps} from "@anthropic/ink";
import {marked} from "marked";
import {highlightCode} from "../theme/syntax.js";
import {colors, displayWidth, padEndWidth, wrap} from "../theme/index.js";

type Props = {content: string; width: number; dimColor?: boolean};

// React context crosses both Box and nested Text boundaries. The themed Ink
// wrapper resolves each span separately, so relying on a parent's terminal
// color can lose the message tone at an inline span or a table cell.
const MarkdownForeground = React.createContext<NonNullable<TextProps["color"]>>(colors.chat.assistantFg);
function Text({color, dimColor, ...props}: TextProps) {
  const foreground = useContext(MarkdownForeground);
  return <InkText {...props} color={dimColor ? colors.chat.reasoningFg : color ?? foreground}/>;
}

const cache = new Map<string, any[]>();
function lex(content: string): any[] {
  const hit = cache.get(content);
  if (hit) return hit;
  const tokens = marked.lexer(content, {gfm: true}) as any[];
  cache.set(content, tokens);
  if (cache.size > 300) cache.delete(cache.keys().next().value as string);
  return tokens;
}

function plain(token: any): string {
  if (!token) return "";
  if (typeof token === "string") return token;
  if (Array.isArray(token)) return token.map(plain).join("");
  if (Array.isArray(token.tokens)) return token.tokens.map(plain).join("");
  return String(token.text ?? token.raw ?? "");
}

function Inline({tokens}: {tokens: any[]}) {
  return <>{tokens.map((token, index) => {
    const key = `${token.type}-${index}`;
    if (token.type === "strong") return <Text key={key} bold><Inline tokens={token.tokens || []}/></Text>;
    if (token.type === "em") return <Text key={key} italic><Inline tokens={token.tokens || []}/></Text>;
    if (token.type === "del") return <Text key={key} strikethrough><Inline tokens={token.tokens || []}/></Text>;
    if (token.type === "codespan") return <Text key={key} color={colors.chat.emphasis}>{token.text}</Text>;
    if (token.type === "link") return <Text key={key} underline><Inline tokens={token.tokens || []}/>{token.href ? ` (${token.href})` : ""}</Text>;
    if (token.type === "br") return <Text key={key}>{"\n"}</Text>;
    if (token.tokens) return <Inline key={key} tokens={token.tokens}/>;
    return <Text key={key}>{token.text ?? token.raw ?? ""}</Text>;
  })}</>;
}

function Table({token, width}: {token: any; width: number}) {
  const columns = Math.max(1, token.header?.length || 0, ...(token.rows || []).map((row: any[]) => row.length));
  const capacity = Math.max(2, Math.floor((Math.max(8, width) - 1) / 3));
  const groups: number[][] = [];
  if (columns <= capacity) groups.push(Array.from({length: columns}, (_, index) => index));
  else {
    groups.push(Array.from({length: capacity}, (_, index) => index));
    for (let start = capacity; start < columns; start += capacity - 1) {
      // Keep the row key beside each remaining group of columns. Never turn
      // tabular data into unrelated label/value paragraphs or drop columns.
      groups.push([0, ...Array.from({length: Math.min(capacity - 1, columns - start)}, (_, index) => start + index)]);
    }
  }
  return <Box flexDirection="column">{groups.map((group, index) =>
    <Box key={index} marginTop={index ? 1 : 0}><TableGrid width={width} token={{
      header: token.header?.length ? group.map(column => token.header[column]) : [],
      rows: (token.rows || []).map((row: any[]) => group.map(column => row[column])),
      align: group.map(column => token.align?.[column]),
    }}/></Box>,
  )}</Box>;
}

function TableGrid({token, width}: {token: any; width: number}) {
  const header = token.header || [];
  const rows = token.rows || [];
  const columns = Math.max(1, header.length, ...rows.map((row: any[]) => row.length));
  const matrix = [header, ...rows];
  const minimum = 2; // one complete CJK/emoji grapheme must fit
  const padding = width >= columns * 5 + 1 ? 1 : 0;
  const frameWidth = columns * (1 + padding * 2) + 1;
  const available = Math.max(columns * minimum, Math.floor(width) - frameWidth);
  const widths = Array.from({length: columns}, (_, column) => Math.max(
    minimum,
    Math.min(40, Math.max(...matrix.map((row: any[]) => displayWidth(plain(row[column])) || 0))),
  ));
  while (widths.reduce((sum, value) => sum + value, 0) > available) {
    const widest = widths.reduce((best, value, index) => value > widths[best]! ? index : best, 0);
    if (widths[widest]! <= minimum) break;
    widths[widest]!--;
  }
  // Terminal tables should use the available reading width, not collapse to
  // the shortest cell contents. Distribute spare cells by the columns'
  // natural demand so a label column stays compact while prose columns grow.
  let spare = Math.max(0, available - widths.reduce((sum, value) => sum + value, 0));
  const weights = Array.from({length: columns}, (_, column) => Math.max(
    1,
    ...matrix.map((row: any[]) => displayWidth(plain(row[column])) || 0),
  ));
  const totalWeight = weights.reduce((sum, value) => sum + value, 0);
  if (spare > 0) {
    const grants = weights.map(weight => Math.floor(spare * weight / totalWeight));
    grants.forEach((grant, index) => { widths[index]! += grant; });
    spare -= grants.reduce((sum, value) => sum + value, 0);
    for (let index = columns - 1; spare > 0; index = (index - 1 + columns) % columns) {
      widths[index]! += 1;
      spare -= 1;
    }
  }
  const lines = (cells: any[]) => {
    const wrapped = Array.from({length: columns}, (_, column) =>
      wrap(plain(cells[column]), widths[column]!),
    );
    const height = Math.max(1, ...wrapped.map(cell => cell.length));
    return Array.from({length: height}, (_, lineIndex) => "│" + wrapped.map((cell, column) => {
      const value = cell[lineIndex] || "";
      const spare = Math.max(0, widths[column]! - displayWidth(value));
      const align = token.align?.[column];
      const left = align === "right" ? spare : align === "center" ? Math.floor(spare / 2) : 0;
      return " ".repeat(padding + left) + padEndWidth(value, widths[column]! - left) + " ".repeat(padding);
    }).join("│") + "│");
  };
  const rule = (left: string, middle: string, right: string) => left
    + widths.map(value => "─".repeat(value + padding * 2)).join(middle) + right;
  return <Box flexDirection="column">
    <Text dimColor>{rule("┌", "┬", "┐")}</Text>
    {header.length ? lines(header).map((line, index) => <Text key={`h${index}`} bold>{line}</Text>) : null}
    {header.length ? <Text dimColor>{rule("├", "┼", "┤")}</Text> : null}
    {rows.flatMap((row: any[], rowIndex: number) => lines(row).map((line, lineIndex) =>
      <Text key={`${rowIndex}:${lineIndex}`}>{line}</Text>,
    ))}
    <Text dimColor>{rule("└", "┴", "┘")}</Text>
  </Box>;
}

function Blocks({tokens, width, dimColor}: {tokens: any[]; width: number; dimColor?: boolean}) {
  return <>{tokens.map((token, index) => {
    const key = `${token.type}-${index}`;
    if (token.type === "space") return null;
    if (token.type === "heading") return <Box key={key} marginTop={index ? 1 : 0}><Text bold dimColor={dimColor}><Inline tokens={token.tokens || []}/></Text></Box>;
    if (token.type === "paragraph" || token.type === "text") return <Text key={key} wrap="wrap" dimColor={dimColor}><Inline tokens={token.tokens || [{type: "text", text: token.text || token.raw}]}/></Text>;
    if (token.type === "code") return <Box key={key} flexDirection="column" paddingLeft={1} borderStyle="single" borderLeft borderRight={false} borderTop={false} borderBottom={false} borderColor={colors.named.gray}>
      {token.lang ? <Text dimColor>{token.lang}</Text> : null}
      {dimColor ? <Text dimColor>{String(token.text || "")}</Text>
        : <Ansi>{highlightCode(String(token.text || ""), String(token.lang || ""))}</Ansi>}
    </Box>;
    if (token.type === "blockquote") return <Box key={key} paddingLeft={1} borderStyle="single" borderLeft borderRight={false} borderTop={false} borderBottom={false} borderColor={colors.named.gray}>
      <Blocks tokens={token.tokens || []} width={Math.max(8, width - 2)} dimColor/>
    </Box>;
    if (token.type === "list") return <Box key={key} flexDirection="column">
      {(token.items || []).map((item: any, itemIndex: number) => <Box key={itemIndex} flexDirection="row">
        <Text dimColor>{token.ordered ? `${Number(token.start || 1) + itemIndex}. ` : "• "}</Text>
        <Box flexDirection="column" flexGrow={1}><Blocks tokens={item.tokens || []} width={Math.max(8, width - 3)} dimColor={dimColor}/></Box>
      </Box>)}
    </Box>;
    if (token.type === "table") return <Table key={key} token={token} width={width}/>;
    if (token.type === "hr") return <Text key={key} dimColor>{"─".repeat(Math.max(1, width))}</Text>;
    return <Text key={key} wrap="wrap" dimColor={dimColor}>{plain(token)}</Text>;
  })}</>;
}

export function MarkdownText({content, width, dimColor}: Props) {
  const tokens = useMemo(() => lex(content), [content]);
  return <MarkdownForeground.Provider value={dimColor ? colors.chat.reasoningFg : colors.chat.assistantFg}>
    <Box flexDirection="column" width={Math.max(8, width)}><Blocks tokens={tokens} width={width} dimColor={dimColor}/></Box>
  </MarkdownForeground.Provider>;
}

/** Reparse only the growing final top-level block during token streaming. */
export function StreamingMarkdownText({content, width, dimColor}: Props) {
  const stableRef = useRef("");
  if (!content.startsWith(stableRef.current)) stableRef.current = "";
  const boundary = stableRef.current.length;
  const tokens = marked.lexer(content.slice(boundary), {gfm: true}) as any[];
  let last = tokens.length - 1;
  while (last >= 0 && tokens[last]?.type === "space") last -= 1;
  let advance = 0;
  for (let index = 0; index < last; index += 1) advance += String(tokens[index]?.raw || "").length;
  if (advance > 0) stableRef.current = content.slice(0, boundary + advance);
  const stable = stableRef.current;
  const growing = content.slice(stable.length);
  return <Box flexDirection="column" width={Math.max(8, width)}>
    {stable ? <MarkdownText content={stable} width={width} dimColor={dimColor}/> : null}
    {growing ? <MarkdownText content={growing} width={width} dimColor={dimColor}/> : null}
  </Box>;
}
