// Both product renderers use this Dracula mapping, with no implicit background.
// Scope mapping follows https://github.com/dracula/highlightjs; the palette is
// owned by tokens.ts. Truecolor avoids dependence on the terminal ANSI palette.
import chalk from "chalk";
import {DEFAULT_THEME, highlight, supportsLanguage, type Theme} from "cli-highlight";
import {colors} from "./tokens.js";

const palette = colors.syntax;
let cached: {level: number; theme: Theme} | undefined;

function syntaxTheme(): Theme {
  if (cached?.level === chalk.level) return cached.theme;
  const foreground = chalk.hex(palette.foreground);
  const theme: Theme = {
    ...Object.fromEntries(Object.keys(DEFAULT_THEME).map(key => [key, foreground])),
    default: foreground,
    keyword: chalk.hex(palette.pink).bold,
    built_in: chalk.hex(palette.cyan),
    "selector-tag": chalk.hex(palette.cyan).bold,
    section: chalk.hex(palette.cyan).bold,
    link: chalk.hex(palette.cyan),
    title: chalk.hex(palette.green).italic.bold,
    attr: chalk.hex(palette.green).italic,
    "meta-keyword": chalk.hex(palette.green).italic,
    string: chalk.hex(palette.yellow),
    meta: chalk.hex(palette.yellow),
    name: chalk.hex(palette.yellow).bold,
    type: chalk.hex(palette.yellow).bold,
    symbol: chalk.hex(palette.yellow),
    bullet: chalk.hex(palette.yellow),
    variable: chalk.hex(palette.yellow),
    "template-tag": chalk.hex(palette.yellow),
    "template-variable": chalk.hex(palette.yellow),
    comment: chalk.hex(palette.comment),
    quote: chalk.hex(palette.comment),
    literal: chalk.hex(palette.purple).bold,
    number: chalk.hex(palette.purple),
    regexp: chalk.hex(palette.red),
    addition: chalk.hex(palette.green),
    deletion: chalk.hex(palette.red),
    strong: foreground.bold,
    emphasis: foreground.italic,
  };
  cached = {level: chalk.level, theme};
  return theme;
}

export function highlightCode(source: string, language?: string, autoDetect = true): string {
  const known = Boolean(language && supportsLanguage(language));
  if (!known && !autoDetect) return source;
  try {
    return highlight(source, {
      ...(known ? {language} : {}),
      ignoreIllegals: true,
      theme: syntaxTheme(),
    });
  } catch {
    return source;
  }
}
