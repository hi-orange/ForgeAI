import { useEffect, useState } from 'react'

export function HomePage() {
  const [token, setToken] = useState(localStorage.getItem('qa-token') || '')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [title, setTitle] = useState('')
  const [items, setItems] = useState<{id: number; title: string}[]>([])
  const [message, setMessage] = useState('')
  const load = async () => {
    const response = await fetch('/api/v1/records', { headers: { Authorization: `Bearer ${token}` } })
    if (response.ok) setItems((await response.json()).items)
  }
  useEffect(() => { if (token) void load() }, [token])
  return <main>
    <h1>Private records</h1>
    {!token ? <form onSubmit={async event => {
      event.preventDefault()
      const response = await fetch('/api/v1/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, password }) })
      if (!response.ok) { setMessage('Wrong credentials'); return }
      const result = await response.json()
      localStorage.setItem('qa-token', result.token)
      setToken(result.token)
      setMessage('')
    }}>
      <input data-testid="username" value={username} onChange={e => setUsername(e.target.value)} />
      <input data-testid="password" type="password" value={password} onChange={e => setPassword(e.target.value)} />
      <button data-testid="login">Log in</button>
    </form> : <>
      <p>Signed in as {token}</p>
      <button data-testid="logout" onClick={() => { localStorage.removeItem('qa-token'); setToken(''); setItems([]) }}>Log out</button>
      <form onSubmit={async event => {
        event.preventDefault()
        const response = await fetch('/api/v1/records', { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify({ title }) })
        if (!response.ok) { setMessage('Invalid title'); return }
        await load()
      }}>
        <input data-testid="title" value={title} onChange={e => setTitle(e.target.value)} />
        <button data-testid="save">Save</button>
      </form>
      <ul>{items.map(item => <li key={item.id}>{item.title}</li>)}</ul>
      {items.length === 0 && <p>No records</p>}
    </>}
    <p>{message}</p>
  </main>
}

export default HomePage
