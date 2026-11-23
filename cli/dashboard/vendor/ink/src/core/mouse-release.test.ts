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

for(const action of [false,true])test(`rapid action clicks stay clicks while text multi-click selection is preserved (action=${action})`,()=>{
 const selection=createSelectionState();let clicks=0,multi=0;
 const app:any={props:{selection,onSelectionChange(){},onHoverAt(){},onSelectionDrag(){},isActionAt(){return action;},
  onClickAt(){clicks++;return true;},onMultiClick(){multi++;selection.anchor={col:2,row:2};selection.focus={col:4,row:2};}},clickCount:0,lastClickTime:0};
 for(let i=0;i<4;i++)for(const sequence of ["\x1b[<0;3;3M","\x1b[<0;3;3m"]){
  const [events]=parseMultipleKeypresses({...INITIAL_STATE},sequence);for(const event of events)handleMouseEvent(app,event as ParsedMouse);
 }
 expect(clicks).toBe(action?4:1);expect(multi).toBe(action?0:3);
});

for(const release of ['\x1b[<0;30;20m','\x1b[<3;30;20M','\x1b[<35;30;20M'])test(`captured control drag survives leaving its rect and terminates on ${JSON.stringify(release)}`,()=>{
 const selection=createSelectionState(),events:string[]=[];let selected=0;
 const app:any={props:{selection,onSelectionChange(){},onHoverAt(){},onMultiClick(){},
  onSelectionDrag(){selected++;},captureMouseAt(){return(type:string)=>events.push(type);}},clickCount:0,lastClickTime:0};
 for(const sequence of ['\x1b[<0;2;2M','\x1b[<32;30;20M',release,'\x1b[<32;2;2M']){
  const [parsed]=parseMultipleKeypresses({...INITIAL_STATE},sequence);for(const event of parsed)handleMouseEvent(app,event as ParsedMouse);
 }
 expect(events).toEqual(['mousedrag','mouseup']);expect(app.mouseCapture).toBeUndefined();expect(selected).toBe(1);
 expect(selection.isDragging).toBe(false);
});
