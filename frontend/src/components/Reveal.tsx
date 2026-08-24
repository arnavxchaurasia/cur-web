import type { ReactNode } from 'react'
import { useInView } from '../lib/useInView'

type Props = Readonly<{
  children: ReactNode
  delay?: number
  className?: string
}>

export function Reveal({ children, delay = 0, className = '' }: Props) {
  const { ref, visible } = useInView()
  return (
    <div
      ref={ref}
      className={`reveal ${visible ? 'reveal-visible' : ''} ${className}`}
      style={{ transitionDelay: visible ? `${delay}ms` : '0ms' }}
    >
      {children}
    </div>
  )
}
