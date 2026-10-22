export function BrandMark({ className = '' }: { className?: string }) {
  return (
    <svg viewBox="0 0 40 40" fill="none" aria-hidden="true" className={className}>
      <path d="M6 8h8l12 24h8M6 20h28M6 32h8L26 8h8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="square" />
      <path d="M14 8v24M26 8v24" stroke="currentColor" strokeWidth=".65" opacity=".5" />
      <circle cx="20" cy="20" r="3" fill="currentColor" />
    </svg>
  )
}

export function WeaveLines({ className = '' }: { className?: string }) {
  return (
    <svg viewBox="0 0 540 220" fill="none" aria-hidden="true" className={className}>
      {Array.from({ length: 11 }, (_, index) => (
        <path key={index} d={`M${35 + index * 12} 240 C ${70 + index * 15} ${45 - index * 6}, ${250 + index * 10} ${230 - index * 12}, ${405 + index * 13} -20`}
          stroke="currentColor" strokeWidth={index === 5 ? '1.5' : '.65'} opacity={index === 5 ? '.8' : '.32'} />
      ))}
      <path d="M55 165H475M95 110H495M150 55H515" stroke="currentColor" strokeWidth=".5" opacity=".18" />
      <circle cx="305" cy="110" r="4" fill="currentColor" opacity=".7" />
      <circle cx="305" cy="110" r="13" stroke="currentColor" opacity=".25" />
    </svg>
  )
}
