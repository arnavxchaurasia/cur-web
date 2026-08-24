import { useEffect } from 'react'

const SUFFIX = ' · AWS → GCP Estimator'

export function useTitle(prefix: string) {
  useEffect(() => {
    const prev = document.title
    document.title = prefix + SUFFIX
    return () => { document.title = prev }
  }, [prefix])
}
