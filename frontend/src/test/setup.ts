import '@testing-library/jest-dom/vitest'
import { cleanup, configure } from '@testing-library/react'
import { afterEach, vi } from 'vitest'

// findBy*/waitFor give up after 1 s by default — too short for a loaded machine (CI runner, backend tests alongside)
configure({ asyncUtilTimeout: 10000 })

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  localStorage.clear()
})
