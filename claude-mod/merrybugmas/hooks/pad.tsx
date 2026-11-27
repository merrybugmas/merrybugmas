import type { ClientModule, JsonValue } from 'claude-code'

/**
 * 게임 화면 위에 투명하게 덮는 입력판: 아무것도 그리지 않는다.
 * 게임 화면을 한 번 클릭하면 키보드(방향키·Enter·Tab·⌫·글자)가 그대로 게임으로 가고,
 * 클릭한 칸 좌표도 게임에 넘겨 메뉴·쥐·게시판·우리를 누를 수 있다. Esc 는 입력창으로 돌아간다.
 */
type Ev = { id: number; key?: string; click?: [number, number] }
type State = { n: number; events: Ev[] }

const Pad: ClientModule<JsonValue, State> = (_props, s) => {
  const { Box } = s.elements
  if (s.state === undefined) {
    const st: State = { n: 0, events: [] }
    const push = (ev: Omit<Ev, 'id'>) => {
      st.n += 1
      st.events = [...st.events, { id: st.n, ...ev }].slice(-12)
      s.post({ events: st.events } as unknown as JsonValue)
    }
    s.onKey(k => push({ key: k.key }))
    s.onPointer(p => { if (p.type === 'down') push({ click: [p.x, p.y] }) })
    s.setState(st)
  }
  return <Box width={s.columns || 1} height={s.rows || 1} />
}

export default Pad
