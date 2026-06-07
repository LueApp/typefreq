"""Spell-check pipeline.

Pure logic: check whether a word is misspelled and decide whether to notify.
The actual notification dispatch (overlay popup, notify-send, log line, …) is
done by whoever owns the SpellNotifier — we only return True/False so dispatch
can happen on the right thread.

Rate limits:
 - words shorter than TYPO_MIN_LEN are never reported
 - the same misspelled word won't be reported twice within TYPO_COOLDOWN_SEC
 - no more than one notification globally within TYPO_RATE_LIMIT_SEC
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from threading import Lock

from spellchecker import SpellChecker

from .config import LANG, TYPO_COOLDOWN_SEC, TYPO_MIN_LEN, TYPO_RATE_LIMIT_SEC

log = logging.getLogger("typefreq.spell")


_ENGLISH_SPELLING_VARIANTS = frozenset(
    """
    aeroplane aeroplanes
    aluminium
    anaemia anaemic
    anaesthesia anaesthetic
    analyse analysed analyser analysers analyses analysing
    apologised apologises apologising apologise
    behaviour behaviours
    calibre calibres
    cancelled cancelling cancellation
    candour
    catalogue catalogued catalogues cataloguing
    centre centred centres centring
    cheque cheques
    colour coloured colouring colours
    defence defences
    dialogue dialogues
    diarrhoea
    draught draughts
    endeavour endeavoured endeavouring endeavours
    encyclopaedia encyclopaedias
    favoured favouring favourite favourites favour favourable favours
    fibre fibres
    foetus foetuses
    flavour flavoured flavouring flavours
    grey greyer greyest
    harbour harboured harbouring harbours
    honour honoured honouring honours honourable
    humour humoured humouring humours
    jewellery jeweller jewellers
    kerb kerbs
    labour laboured labouring labours
    licence licences
    litre litres
    manoeuvre manoeuvred manoeuvres manoeuvring
    meagre
    metre metres
    minimise minimised minimises minimising
    mould moulded moulding moulds
    moustache moustaches
    neighbour neighboured neighbourhood neighbourhoods neighbouring neighbours
    odour odours
    offence offences
    organise organised organiser organisers organises organising organisation organisations
    orthopaedic orthopaedics
    paediatric paediatrics paediatrician paediatricians
    paralyse paralysed paralyses paralysing
    plough ploughed ploughing ploughs
    practise practised practises practising
    pretence pretences
    prioritise prioritised prioritises prioritising
    programme programmes
    pyjamas
    rancour
    realised realises realising realisation realise
    recognised recognises recognising recognisable recognise
    rigour rigorous
    rumour rumoured rumouring rumours
    saviour saviours
    savour savoured savouring savours savoury
    sceptic sceptical scepticism sceptics
    smoulder smouldered smouldering smoulders
    sombre
    specialise specialised specialises specialising
    splendour
    standardise standardised standardises standardising
    sulphur sulphurous
    theatre theatres
    travelled traveller travellers travelling
    tyre tyres
    valour
    vapour vapours
    vigour vigorous
    visualise visualised visualises visualising
    """.split()
)


def _uses_english_dictionary(lang: str | Iterable[str] | None) -> bool:
    if lang is None:
        return False
    if isinstance(lang, str):
        return lang.lower() == "en"
    return any(str(item).lower() == "en" for item in lang)


class SpellNotifier:
    """Thread-safe spell-checker + notification rate limiter.

    Holds a reference to a `custom_words` set that the Engine owns and
    mutates. Words in that set are treated as correctly spelled regardless
    of what pyspellchecker thinks — useful for names, jargon, project-
    specific terms, etc. The set is shared by reference, so updates by
    the API are visible here without further plumbing.
    """

    def __init__(
        self,
        lang: str = LANG,
        custom_words: set[str] | None = None,
    ) -> None:
        self._spell = SpellChecker(language=lang)
        self._lock = Lock()
        self._last_seen: dict[str, float] = {}
        self._last_notify_at = 0.0
        self._custom: set[str] = custom_words if custom_words is not None else set()
        self._accepted_variants = (
            _ENGLISH_SPELLING_VARIANTS if _uses_english_dictionary(lang) else frozenset()
        )
        self.checked = 0
        self.notified = 0

    def check(self, word: str) -> tuple[bool, str | None]:
        """Return (is_misspelled, suggestion_or_none) without changing rate-limit state."""
        self.checked += 1
        if len(word) < TYPO_MIN_LEN:
            return False, None
        # User-whitelisted words override pyspellchecker. Comparison is on the
        # normalized (lowercased) form, which is what callers always pass.
        if word in self._custom:
            return False, None
        # pyspellchecker's bundled English dictionary is mostly US English.
        # Treat common UK/Commonwealth variants as correct words so dialect
        # differences don't get recorded as typos.
        if word in self._accepted_variants:
            return False, None
        if word in self._spell:
            return False, None
        suggestion = self._spell.correction(word)
        if suggestion is None or suggestion == word:
            return False, None
        return True, suggestion

    def should_notify(self, word: str) -> bool:
        """Return True if the caller should send a notification right now.

        Updates internal rate-limit state on True. Safe to call from any thread.
        """
        now = time.monotonic()
        with self._lock:
            last = self._last_seen.get(word, 0.0)
            if now - last < TYPO_COOLDOWN_SEC:
                return False
            if now - self._last_notify_at < TYPO_RATE_LIMIT_SEC:
                return False
            self._last_seen[word] = now
            self._last_notify_at = now
            self.notified += 1
        return True
