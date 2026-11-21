// Original 24 px line icons. currentColor follows the workbench theme; no image requests.
const overlay='<rect x="15" y="5" width="7" height="14" rx="1" fill="currentColor" fill-opacity=".3"/><path d="M12 12H2m4-4-4 4 4 4"/>';
const flip='<path d="m3 17 5-10v10Zm18 0L16 7v10Z" fill="currentColor" fill-opacity=".12"/><path d="M12 3v3m0 4v4m0 4v3" opacity=".55"/>';
const paths={
  split:'<rect x="3" y="4" width="7" height="16" rx="2"/><rect x="14" y="4" width="7" height="16" rx="2" opacity=".5"/>',
  wipe:'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M12 4h7a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-7Z" fill="currentColor" fill-opacity=".25" stroke="none"/><path d="M12 2v20"/>',
  diff:'<rect x="3" y="4" width="18" height="16" rx="2" opacity=".45"/><path d="m12 7 6 10H6Z" fill="currentColor" fill-opacity=".15"/>',
  left:overlay,right:`<g transform="rotate(180 12 12)">${overlay}</g>`,
  up:`<g transform="rotate(90 12 12)">${overlay}</g>`,down:`<g transform="rotate(-90 12 12)">${overlay}</g>`,
  rotate:'<rect x="8" y="9" width="13" height="12" rx="1"/><path d="M3 11V9a5 5 0 0 1 5-5h7m-3-3 3 3-3 3"/>',
  'flip-x':flip,'flip-y':`<g transform="rotate(90 12 12)">${flip}</g>`,
  position:'<path d="M3 9V3h6"/><rect x="7" y="7" width="13" height="13" rx="1" opacity=".4"/><rect x="7" y="7" width="7" height="9" rx="1" fill="currentColor" fill-opacity=".15"/>',
  size:'<rect x="3" y="7" width="8" height="13" rx="1"/><rect x="14" y="7" width="7" height="13" rx="1"/><path d="M3 3h18M6 1 3 3l3 2m12-4 3 2-3 2" opacity=".6"/>',
  brightness:'<circle cx="10" cy="14" r="4"/><path d="M10 6v1m0 14v1M2 14h1m14 0h1M4.3 8.3l.7.7m10 10 .7.7M4.3 19.7l.7-.7"/><path d="m18 2 1.2 3.8L23 7l-3.8 1.2L18 12l-1.2-3.8L13 7l3.8-1.2Z" fill="currentColor" fill-opacity=".15"/>',
  aligned:'<rect x="3" y="3" width="7" height="6" rx="1"/><rect x="14" y="3" width="7" height="6" rx="1"/><path d="M3 13h7m4 0h7M3 18h7m4 0h7" opacity=".45"/>',
  within:'<rect x="3" y="3" width="7" height="6" rx="1"/><rect x="3" y="13" width="7" height="6" rx="1"/><path d="M14 3h7m-7 5h7m-7 5h7m-7 5h7" opacity=".45"/>',
  sort:'<path d="M5 4v16m-3-3 3 3 3-3M12 5h4m-4 7h7m-7 7h10"/>',
  edge:'<path d="M4 3v18M8 4h12v16H8"/><path d="m8 16 4-8 3 6 3-4" opacity=".6"/>',
  histogram:'<path d="M3 3v18h18" opacity=".45"/><path d="m4 19 3-7 3 4 4-11 3 8 4-3v9Z" fill="currentColor" fill-opacity=".2"/>',
  fit:'<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5" opacity=".5"/><rect x="7" y="8" width="10" height="8" rx="1"/>',
  fill:'<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/><path d="M3 3l6 6m6 6 6 6M21 3l-6 6m-6 6-6 6" opacity=".5"/>',
  pixel:'<path d="M4 8 6 6v12m10-10 2-2v12"/><circle cx="12" cy="9" r=".7" fill="currentColor" stroke="none"/><circle cx="12" cy="15" r=".7" fill="currentColor" stroke="none"/>',
  share:'<circle cx="5" cy="12" r="3"/><circle cx="18" cy="5" r="3"/><circle cx="18" cy="19" r="3"/><path d="m8 11 7-4m-7 6 7 4"/>',
} as const;

export function icon(name:keyof typeof paths):SVGSVGElement {
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('width','20');svg.setAttribute('height','20');
  svg.setAttribute('fill','none');svg.setAttribute('stroke','currentColor');svg.setAttribute('stroke-width','1.65');
  svg.setAttribute('stroke-linecap','round');svg.setAttribute('stroke-linejoin','round');
  svg.setAttribute('aria-hidden','true');svg.setAttribute('focusable','false');svg.innerHTML=paths[name];
  return svg;
}
