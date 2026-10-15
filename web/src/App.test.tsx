import { render, screen } from '@testing-library/react'
import App from './App'

describe('App', () => {
  it('renders the console shell', () => {
    render(<App />)
    expect(screen.getByRole('heading', { name: 'Agent Platform' })).toBeInTheDocument()
  })
})
