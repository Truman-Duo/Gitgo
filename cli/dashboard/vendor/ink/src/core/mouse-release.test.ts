import {expect, test} from "bun:test";
import {parseMultipleKeypresses, INITIAL_STATE, type ParsedMouse} from "./parse-keypress.js";
import {handleMouseEvent} from "../components/App.js";
import {createSelectionState, updateSelection} from "./selection.js";

const x10 = (button: number, col: number, row: number) => "\x1b[M" + String.fromCharCode(button + 32, col + 32, row + 32);

for (const release of ["\x1b[<0;5;4m", "\x1b[<3;5;4M", x10(3, 5, 4)]) {
  test(`mouse release ${JSON.stringify(release)} ends dragging without clearing selected text`, () => {
    const selection = createSelectionState();
    const app: any = {props: {selection, onSelectionChange() {},
      onSelectionDrag(col: number, row: number) {updateSelection(selection, col, row);},
      onHoverAt() {}, onMultiClick() {}}, clickCount: 0, lastClickTime: 0};
    let state = {...INITIAL_STATE};
    for (const data of ["\x1b[<0;2;2M", "\x1b[<32;5;4M", release]) {
      // Real streaming tokenizer, including a fragmented release sequence.
      for (const byte of data) {
        const [events, next] = parseMultipleKeypresses(state, byte);
        state = next;
        for (const event of events) {
          expect(event.kind).toBe("mouse");
          handleMouseEvent(app, event as ParsedMouse);
        }
      }
    }
    expect(selection.isDragging).toBe(false);
    expect(selection.anchor).toEqual({col: 1, row: 1});
    expect(selection.focus).toEqual({col: 4, row: 3});
  });
}

test("legacy motion without a pressed button remains hover and wheel remains a key", () => {
  const [events] = parseMultipleKeypresses({...INITIAL_STATE}, x10(35, 5, 4) + x10(64, 5, 4));
  expect(events[0]?.kind).toBe("mouse");
  expect((events[0] as ParsedMouse).action).toBe("press");
  expect(events[1]?.kind).toBe("key");
});
