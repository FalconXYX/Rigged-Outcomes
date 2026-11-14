#!/usr/bin/env python3
"""
Offline tests for the shared vocabulary of the closed and the open list
(watchlist.kinds): no network.

    python3 tests/test_watchlist_kinds.py

Covers the embargo rule (all 15 embargoed events of the closed list's old
replay, Oct 31 - Nov 12, and nothing else of its 201; none of the seven
featured cases), the filing of the review's corrected examples (final.md
section 2), the dated place, the odds and betting headline filter, step and
leak words, source ranks, the word-for-word copy check, the card sentences
(the proposal's own five cards; no sentence ever says "bet"), and that
watchlist.upsets still uses the same rules.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from watchlist import kinds as K  # noqa: E402
from watchlist import upsets as up  # noqa: E402

# The closed list's old replay: one (question, event title) per event, the 201 events that closed
# Oct 31 - Nov 12, 2026 (scratchpad proposal/testset.json, built by proposal/s01_closed_set.py).
REPLAY_EVENTS = [
    ("Will \"Wild Horse Nine\" score at least 92 on the Rotten Tomatoes Tomatometer?",
     "\"Wild Horse Nine\" Rotten Tomatoes Score?"),
    ("Will there be 5 or more ChatGPT outages in October 2026?",
     "# of ChatGPT Outage Days in October 2026?"),
    ("Alexandra Leclerc pregnant in 2026?",
     "Alexandra Leclerc pregnant in 2026?"),
    ("Allstate (ALL) Q3 property-liability combined ratio 86% to 88%?",
     "Allstate (ALL) Q3 property-liability combined ratio?"),
    ("Another Iranian Ambassador expelled by October 31, 2026?",
     "Another Iranian Ambassador expelled by...?"),
    ("Another NATO Article 4 invocation by October 31, 2026?",
     "Another NATO article 4 by...?"),
    ("Will a new pneumonic plague case be confirmed in Russia by October 31?",
     "Another pneumonic plague case in Russia by...?"),
    ("Will Anthropic IPO by October 31, 2026?",
     "Anthropic IPO by __?"),
    ("Will Argentina's monthly inflation in October 2026 be between 1.9% and 2.1%?",
     "Argentina Monthly Inflation - October"),
    ("Bab el-Mandeb Strait effectively closed by October 31?",
     "Bab el-Mandeb Strait effectively closed by...?"),
    ("Will the Bank of Brazil make no change to the target for the Selic rate at the November meeting?",
     "Bank of Brazil decision in November?"),
    ("Bank of England increases interest rates by 25 bps after November 2026 meeting?",
     "Bank of England decision in November?"),
    ("Bank of England rate hike in 2026?",
     "Bank of England rate hike in 2026?"),
    ("Bank of Mexico decreases interest rates by 25 bps after November 2026 meeting?",
     "Bank of Mexico Decision in November?"),
    ("Will Xiaomi have the best Chinese AI model at the end of October 2026?",
     "Best Chinese AI Company end of October?"),
    ("Carles Puigdemont back in Spain by October 31, 2026?",
     "Carles Puigdemont back in Spain by...?"),
    ("Will Claude Fable 5.2 debut with an HLE-Diamond score of 70% or higher?",
     "Claude Fable 5.2: HLE-Diamond Debut?"),
    ("Will CNN fly with Trump by October 31?",
     "CNN flies with Trump by..?"),
    ("Will Core CPI MoM be 0.3% in October?",
     "Core CPI MoM - October 2026"),
    ("Cornell President out by October 31?",
     "Cornell President out by October 31?"),
    ("Will Julia Stiles be eliminated in week 8 of Dancing With The Stars: Season 35?",
     "Dancing With The Stars S35: Week 8 Eliminations"),
    ("Will Jenna Dewan be eliminated in week 9 of Dancing With The Stars: Season 35?",
     "Dancing With The Stars S35: Week 9 Eliminations"),
    ("Will the Democrats fail to sweep the House and Senate and the US government take a stake in OpenAI or Anthropic by the end of 2027?",
     "Dem sweep & US stake in OpenAI or Anthropic by end of 2027?"),
    ("Will the Democrats fail to sweep the House and Senate and the US-Iran ceasefire not hold through 2027?",
     "Dem sweep & US-Iran ceasefire through 2027?"),
    ("Will the federal gas tax be suspended by November 2, 2026?",
     "Federal Gas Tax Suspended by...?"),
    ("Fetterman leaves the Democrats before the midterms?",
     "Fetterman leaves the Democrats by...?"),
    ("FlyDubai co-pilot transferred to Israel by Oct. 31?",
     "FlyDubai co-pilot transferred to Israel by Oct. 31?"),
    ("Flávio Bolsonaro charged or arrested by October 31?",
     "Flávio Bolsonaro charged or arrested by Brazilian authorities by October 31?"),
    ("France declares state of emergency by October 31, 2026?",
     "France declares state of emergency by...?"),
    ("France declares state of siege (martial law) by October 31, 2026?",
     "France declares state of siege (martial law) by...?"),
    ("Friedrich Merz out as Chancellor of Germany before October 31, 2026?",
     "Friedrich Merz out as Chancellor of Germany by...?"),
    ("Gemini Carbon released by October 31, 2026?",
     "Gemini Carbon released by...?"),
    ("Will Gemini Carbon debut with an HLE-Diamond score of 60% or higher?",
     "Gemini Carbon: HLE-Diamond Debut?"),
    ("Will Gemini Carbon debut at a score of at least 1520 by June 30, 2027?",
     "Gemini Carbon: Text Arena Debut?"),
    ("Next Astra Model (6.1+) released by October 31, 2026?",
     "GPT Astra 6.1+ released by...?"),
    ("Will GPT Astra 6.1+ be released on November 6, 2026?",
     "GPT Astra 6.1+ released on...?"),
    ("GTA VI released by October 31?",
     "GTA VI released by...?"),
    ("Will the highest score achieved on Humanity’s Last Exam in 2026 be 60% or higher?",
     "Highest score on Humanity’s Last Exam in 2026?"),
    ("Will Holtec Nuclear not IPO before November 2026?",
     "Holtec Nuclear IPO Closing Market Cap"),
    ("Will there be Houthi military action against Israel by October 31, 2026?",
     "Houthi military action against Israel by...?"),
    ("Houthi x Saudi Arabia effective ceasefire by October 31?",
     "Houthi x Saudi Arabia effective ceasefire begins by...? (2 week pause)"),
    ("Houthis enter Aden by October 31, 2026?",
     "Houthis enter Aden by...?"),
    ("Will the Houthis enter Saudi Arabia by October 31?",
     "Houthis enter Saudi Arabia by...?"),
    ("Houthis seize an oil tanker by October 31, 2026?",
     "Houthis seize an oil tanker by...?"),
    ("Will the Houthis successfully target shipping by October 31, 2026?",
     "Houthis successfully target shipping by...?"),
    ("Will the US add at least 100k jobs in October?",
     "How many jobs added in October?"),
    ("Will 0 people leave the Trump Cabinet by December 31, 2026?",
     "How many more people leave the Trump cabinet this year?"),
    ("Will UKMTO publish fewer than 4 notices between November 2-November 8?",
     "How many UKMTO notices November 2–8?"),
    ("Will UKMTO publish 8-11 notices between October 26-November 1?",
     "How many UKMTO notices October 26–November 1?"),
    ("IAEA visits Isfahan, Fordow, or Natanz nuclear site by October 31?",
     "IAEA visits Isfahan, Fordow, or Natanz nuclear site by...?"),
    ("Ilhan Omar federally charged by October 31?",
     "Ilhan Omar federally charged by...?"),
    ("Will Anthropic's initial public offering occur in October 2026 (ET)?",
     "In which month will Anthropic IPO?"),
    ("Iran agrees to surrender enriched uranium stockpile by October 31, 2026?",
     "Iran agrees to surrender enriched uranium stockpile by...?"),
    ("Iran blockade lifted before US x Iran ceasefire ends?",
     "Iran blockade lifted before US x Iran ceasefire ends?"),
    ("Iran charges Hormuz fees by October 31?",
     "Iran charges Hormuz fees by...?"),
    ("Iran full airspace closure by October 31, 2026?",
     "Iran full airspace closure by...?"),
    ("Iran invades Kuwait by October 31?",
     "Iran invades Kuwait by...?"),
    ("Iran leadership change by October 31?",
     "Iran leadership change by...?"),
    ("Iran-Oman Hormuz Agreement by October 31?",
     "Iran-Oman Hormuz Management Agreement by...?"),
    ("Will ISM Manufacturing PMI be between 55.0 and 55.9 in October?",
     "ISM Manufacturing PMI - October 2026"),
    ("Israel accuses Iran/proxies of plane stabbing incident by Oct 31?",
     "Israel accuses Iran/proxies of plane stabbing incident by Oct 31?"),
    ("Israel military action against Greater Beirut by October 31?",
     "Israel military action against Beirut by...?"),
    ("Israel military action against Lebanon on October 6?",
     "Israel military action against Lebanon on...?"),
    ("Israel military action against Lebanon on November 8?",
     "Israel military action against Lebanon on...?"),
    ("Israel military action against Syria by October 31, 2026?",
     "Israel military action against Syria by...?"),
    ("Israel military action against Yemen by October 31?",
     "Israel military action against Yemen by...?"),
    ("Israel x Iran ceasefire continues through October 31?",
     "Israel x Iran ceasefire continues through...?"),
    ("Israel x Syria diplomatic meeting by October 31, 2026?",
     "Israel x Syria diplomatic meeting by...?"),
    ("Israeli airspace closed by October 31, 2026?",
     "Israeli airspace closed by...? (Excluding Holiday Closures)"),
    ("Will Israeli forces withdraw from beyond the Litani River by October 31?",
     "Israeli forces withdraw from beyond the Litani River by…?"),
    ("Will Jack Doherty be sentenced to no prison time?",
     "Jack Doherty Prison Time?"),
    ("Jensen Huang announced resignation or removal by November 12, 2026?",
     "Jensen Huang announced resignation or removal by...?"),
    ("Kanye West performs in Russia by October 31?",
     "Kanye West performs in Russia by October 31?"),
    ("Kharg Island no longer under Iranian control by October 31?",
     "Kharg Island no longer under Iranian control by...?"),
    ("Will Kimi K3.1 Pro debut at a score of at least 1470 by June 30, 2027?",
     "Kimi K3.1 Pro: Text Arena Debut?"),
    ("Kimi K4 released by October 31, 2026?",
     "Kimi K4 released by…?"),
    ("Will GitHub's longest critical incident in October 2026 be between 3 and 6 hours?",
     "Longest GitHub critical incident in October?"),
    ("Marco Rubio out as Secretary of State by November 6, 2026?",
     "Marco Rubio out as Secretary of State by...?"),
    ("McDonald's (MCD) Q3 global comparable sales growth 1% to 2%?",
     "McDonald's (MCD) Q3 global comparable sales growth?"),
    ("Microstrategy delisted from MSCI index by December 31?",
     "Microstrategy delisted from MSCI index by...?"),
    ("Mojtaba Khamenei seen in public by October 31?",
     "Mojtaba Khamenei public appearance by...?"),
    ("Moody's or S&P downgrades France by October 31, 2026?",
     "Moody's or S&P downgrades France by...?"),
    ("NATO downs another Russian drone before Russia x Ukraine peace talks?",
     "NATO downs another Russian drone before Russia x Ukraine peace talks?"),
    ("NATO x Russia military clash by October 31, 2026?",
     "NATO x Russia military clash by...?"),
    ("Next Alibaba Qwen Max (3.9+) released by October 31, 2026?",
     "Next Alibaba Qwen Max (3.9+) released by...?"),
    ("Next DeepSeek Pro released by October 31, 2026?",
     "Next DeepSeek Pro released by...?"),
    ("Next Fable Model (5.2+) released by October 30, 2026?",
     "Next Fable Model (5.2+) released by...?"),
    ("Will the next Fable model (5.2+) be released on October 30, 2026?",
     "Next Fable Model (5.2+) released on...?"),
    ("Will the next Google Gemini Pro model be released by October 31, 2026?",
     "Next Google Gemini Pro Model released by...?"),
    ("Will there be no next Meta Muse Spark model release by October 31, 2026?",
     "Next Muse Spark Model (1.4+) released on...?"),
    ("Next White House Press Secretary announced by October 31, 2026?",
     "Next Press Secretary announced by...?"),
    ("Next US-Iran senior diplomatic meeting by October 31, 2026? (copy)",
     "Next US-Iran senior diplomatic meeting by...?"),
    ("Next Z.ai GLM (5.4+) released by October 31, 2026?",
     "Next Z.ai GLM (5.4+) released by...?"),
    ("Nothing Ever Happens: October",
     "Nothing Ever Happens: October"),
    ("Will China’s CPI increase by between 0.7% and 0.8% over the 12 month period ending in October 2026?",
     "October Inflation China - Annual"),
    ("Will monthly inflation increase by 0.4% in October?",
     "October Inflation US - Monthly"),
    ("Will the October 2026 unemployment rate be 4.1%?",
     "October Unemployment Rate"),
    ("Will OpenRouter process 205T or more tokens the week of November 2?",
     "OpenRouter token usage week of November 2?"),
    ("Perim Island no longer under Houthi control by October 31?",
     "Perim Island no longer under Houthi control by...?"),
    ("Pete Hegseth out as Secretary of Defense by October 31?",
     "Pete Hegseth out as Secretary of Defense by October 31?"),
    ("Reserve Bank of Australia increases interest rates by 25 bps at the November 2026 meeting?",
     "Reserve Bank of Australia Decision in November"),
    ("Russia x Ukraine ceasefire agreement by October 31, 2026?",
     "Russia x Ukraine ceasefire agreement by...?"),
    ("Russia x Ukraine ceasefire by October 31, 2026?",
     "Russia x Ukraine Ceasefire by...?"),
    ("Russia-Ukraine peace talks by October 31, 2026?",
     "Russia x Ukraine peace talks by...?"),
    ("Will Saudi airspace be closed by October 31?",
     "Saudi airspace closed by...?"),
    ("Saudi Arabia military action against Yemen on October 6?",
     "Saudi Arabia military action against Yemen on...?"),
    ("Saudi Arabia military action against Yemen on September 26?",
     "Saudi Arabia military action against Yemen on...?"),
    ("Saudi coalition strike in Yemen on November 3?",
     "Saudi coalition strike in Yemen on...?"),
    ("Saudi troops enter Marib Governorate by October 31, 2026?",
     "Saudi troops enter Marib Governorate by...?"),
    ("Saudi troops enter Taiz Governorate by October 31, 2026?",
     "Saudi troops enter Taiz by...?"),
    ("Saudi troops fighting in Yemen by October 31, 2026?",
     "Saudi troops fighting in Yemen by...?"),
    ("Saudi-Houthi ceasefire agreement announced by October 31?",
     "Saudi-Houthi ceasefire agreement announced by...?"),
    ("SCOTUS accepts sports event contract case by October 31, 2026?",
     "SCOTUS accepts sports event contract case by...?     "),
    ("Will Anthropic be the second-best AI lab at the end of October 2026?",
     "Second-best AI Lab end of October?"),
    ("Shipping targeted in Hormuz on September 29, 2026?",
     "Shipping successfully targeted in Hormuz on...?"),
    ("Shipping targeted in Hormuz on October 4, 2026?",
     "Shipping successfully targeted in Hormuz on...?"),
    ("Situational Awareness raises new capital by October 31?",
     "Situational Awareness raises new capital by...?"),
    ("Will SpaceX Starship Flight Test 15 launch by October 31, 2026?",
     "SpaceX Starship Flight Test 15?"),
    ("SSI public model released by October 31, 2026?",
     "SSI public model released by…?"),
    ("Strait of Hormuz traffic returns to normal by October 31?",
     "Strait of Hormuz traffic returns to normal by October 31?"),
    ("Will Taylor Swift's daily YouTube view count hit 25M in October?",
     "Taylor Swift daily view count hits ___ in October?"),
    ("Will Meta be the third-best AI lab at the end of October 2026?",
     "Third-best AI Lab end of October?"),
    ("Will Alibaba be the third-best Math AI lab at the end of October 2026?",
     "Third-best Text Arena Math AI Lab end of October?"),
    ("Over $5M committed to the Mori Finance public sale?",
     "Total commitments for the Mori Finance public sale on MetaDAO"),
    ("Will Trump lower tariffs on Canada by October 31?",
     "Trump announces Canada tariff decrease by...?"),
    ("Will Trump raise tariffs on Canada by October 31?",
     "Trump announces Canada tariff increase by...?"),
    ("U.S. anti-cartel operation outside of the U.S. by October 31?",
     "U.S. anti-cartel operation on foreign soil by...?"),
    ("Will UK GDP growth in Q3 2026 be between 0.6% and 0.7%?",
     "UK GDP growth in Q3 2026 (QoQ)?"),
    ("Ukraine signs peace deal with Russia by October 31?",
     "Ukraine signs peace deal with Russia by October 31?"),
    ("Will the US announce a diesel export ban by October 31?",
     "US Announces Diesel Export ban by...?"),
    ("US announces end of Iranian blockade by October 31, 2026?",
     "US announces end of Iranian blockade by...?"),
    ("US imports Russian diesel before Russia x Ukraine peace talks?",
     "US imports Russian diesel before Russia x Ukraine peace talks?"),
    ("US imports Russian diesel by October 31?",
     "US imports Russian diesel by...?"),
    ("US imposes new sanctions on China by October 31, 2026?",
     "US imposes new sanctions on China by...?"),
    ("Will the U.S. lift CAATSA sanctions on Turkey by October 31, 2026?",
     "US lifts CAATSA sanctions on Turkey by...?"),
    ("US reissues Iran oil sanction relief by October 31?",
     "US reissues Iran oil sales sanction relief by...?"),
    ("US-Iran ceasefire continues through the midterms?",
     "US-Iran ceasefire continues through the midterms?"),
    ("US x Iran ceasefire continues through October 28?",
     "US-Iran ceasefire continues through...?"),
    ("US x Iran effective ceasefire continues through November 8, 2026?",
     "US-Iran effective ceasefire continues through...?"),
    ("US-Iran Final Nuclear Deal by October 31, 2026?",
     "US-Iran Final Nuclear Deal by…?"),
    ("US-Iran Hormuz Agreement by November 2?",
     "US-Iran Hormuz Agreement by...?"),
    ("US x Yemen ceasefire continues through October 31?",
     "US-Yemen ceasefire continues through...?"),
    ("Will the USS Theodore Roosevelt arrive in CENTCOM by October 31, 2026?",
     "USS Theodore Roosevelt arrives in CENTCOM by...?"),
    ("Will Gemini Carbon be released between November 2 and November 8, 2026?",
     "When will Gemini Carbon be released?"),
    ("Will GPT Astra 6.1+ be released between October 26 and November 1, 2026?",
     "When will GPT Astra 6.1+ be released?"),
    ("Will there be no next Google Gemini Pro model release by November 1, 2026?",
     "When will the next Google Gemini Pro model be released?"),
    ("Will the next Russia x Ukraine peace talks take place in the UAE by December 31, 2027?",
     "Where will the next Russia x Ukraine peace talks take place?"),
    ("Google matches Anthropic's evaluator commitment by October 31?",
     "Which AI labs commit to third party vetting by October 31?"),
    ("Will Anthropic have the best AI Agent at the end of October 2026?",
     "Which company has the best AI Agent end of October?"),
    ("Will Google have the best AI model at the end of October 2026?",
     "Which company has the best AI model end of October?"),
    ("Will Anthropic have the best AI model on LiveBench (Coding) at the end of October 2026?",
     "Which company has the best AI model on LiveBench (Coding) end of October?"),
    ("Will Anthropic have the best AI model on LiveBench (Mathematics) at the end of October 2026?",
     "Which company has the best AI model on LiveBench (Mathematics) end of October?"),
    ("Will Anthropic have the best AI model on LiveBench (Overall) at the end of October 2026?",
     "Which company has the best AI model on LiveBench (Overall) end of October?"),
    ("Will Anthropic have the best Code Arena | WebDev AI at the end of October 2026?",
     "Which company has the best Code Arena WebDev AI model end of October?"),
    ("Will OpenAI have the best Image Edit AI at the end of October 2026?",
     "Which company has the best Image Edit AI end of October?"),
    ("Will MiniMax have the best Image-to-Video AI at the end of October 2026?",
     "Which company has the best Image-to-Video AI end of October?"),
    ("Will OpenAI have the best Text-to-Image AI at the end of October 2026?",
     "Which company has the best Text-to-Image AI end of October?"),
    ("Will Google have the best Text-to-Video AI at the end of October 2026?",
     "Which company has the best Text-to-Video AI end of October?"),
    ("Will Strait of Hormuz traffic return to normal in October?",
     "Which month will Strait of Hormuz traffic return to normal?"),
    ("Will Bernie endorse James Talarico for TX-Sen by Nov 2 2026 ET?",
     "Who will Bernie endorse?"),
    ("Will Dua Lipa headline Coachella 2027?",
     "Who will headline Coachella 2027?"),
    ("Will Susie Wiles leave the Trump administration before 2027?",
     "Who will leave Trump Administration before 2027?"),
    ("Will Trump meet with Javier Milei by October 31, 2026?",
     "Who will Trump meet with by October 31?"),
    ("Will Donald Trump publicly insult Ken Paxton by October 31, 2026?",
     "Who will Trump publicly insult by October 31?"),
    ("Will Trump speak to Friedrich Merz by November 30?",
     "Who will Trump speak to by November 30?"),
    ("Will Trump speak to Ahmed al-Sharaa by October 31?",
     "Who will Trump speak to by October 31?"),
    ("Will 1win launch a token by October 31, 2026?",
     "Will 1win launch a token by ___?"),
    ("Will any aircraft land or take off at Kyiv Boryspil Airport by October 31?",
     "Will any aircraft land or take off at Kyiv Boryspil Airport by...?"),
    ("Will any country join the Mecca Agreement by October 31, 2026?",
     "Will any country join the Mecca Agreement by...?"),
    ("Will Apple release a touchscreen MacBook in 2026?",
     "Will Apple release a touchscreen MacBook in 2026?"),
    ("Will Claude go down 5-7 times in October?",
     "Will Claude go down on __ days in October?"),
    ("Will Iran sabotage undersea internet cables by October 31?",
     "Will Iran damage undersea internet cables by...?"),
    ("Will Iran target an Arab country by November 30, 2026?",
     "Will Iran target an Arab country by...?"),
    ("Will Iran target Jordan by October 31, 2026?",
     "Will Iran target Jordan by...?"),
    ("Will Iran target the United Arab Emirates by October 31, 2026?",
     "Will Iran target the United Arab Emirates by...?"),
    ("Will Lauren Boebert resign by October 31, 2026?",
     "Will Lauren Boebert resign by...?"),
    ("Will OpenAI launch a new consumer hardware product by October 31, 2026?",
     "Will OpenAI launch a consumer hardware product by...?"),
    ("Will Predict.fun launch a token by October 31, 2026?",
     "Will Predict.fun launch a token by ___?"),
    ("Will Puffpaw launch a token by December 31, 2026?",
     "Will Puffpaw launch a token by ___?"),
    ("Will pump.fun buybacks hit $500M by December 31?",
     "Will pump.fun buybacks hit $500M by December 31?"),
    ("Will Russia announce a new forced mobilization by October 31, 2026?",
     "Will Russia announce a new forced mobilization by...?"),
    ("Will Russia capture all of Kostyantynivka by October 31, 2026?",
     "Will Russia capture all of Kostyantynivka by...?"),
    ("Will Russia capture Bilytske by October 31, 2026?",
     "Will Russia capture Bilytske by...?"),
    ("Will Russia capture Dobropillia by October 31, 2026?",
     "Will Russia capture Dobropillia by...?"),
    ("Will Russia capture Kucheriv Yar by October 31?",
     "Will Russia capture Kucheriv Yar by...?"),
    ("Will Russia capture Novyi Donbas by October 31?",
     "Will Russia capture Novyi Donbas by...?"),
    ("Will Russia capture Svitle by October 31?",
     "Will Russia capture Svitle by...?"),
    ("Will Russia capture the Royal Café Alex in Kostyantynivka by October 31, 2026?",
     "Will Russia capture the Royal Café Alex in Kostyantynivka by...?"),
    ("Will Russia capture Vodianske by December 31?",
     "Will Russia capture Vodianske by...?"),
    ("Will Russia enter Oleksiievo-Druzhkivka by October 31?",
     "Will Russia enter Oleksiievo-Druzhkivka again by...?"),
    ("Will Russia target Kyiv on October 28, 2026?",
     "Will Russia target Kyiv on...?"),
    ("Will Russia target Kyiv on November 11, 2026?",
     "Will Russia target Kyiv on...?"),
    ("Will Talarico and Paxton debate live by October 31?",
     "Will Talarico and Paxton debate live by...?"),
    ("Will the Iranian regime fall by October 31?",
     "Will the Iranian regime fall by October 31?"),
    ("Will the US confirm that aliens exist by October 31?",
     "Will the US confirm that aliens exist by...?"),
    ("Will Trump visit Greenland by October 31?",
     "Will Trump visit Greenland by...?"),
    ("Will Ukraine re-enter Huliaipole by October 31, 2026?",
     "Will Ukraine re-enter Huliaipole by...?"),
    ("Will Ukraine re-enter Terny by October 31, 2026?",
     "Will Ukraine re-enter Terny by...?"),
    ("Will US crude oil reserves fall to 280M by October 30, 2026?",
     "Will US crude oil reserves fall to __ by October 30?"),
    ("Will Vivek Ramaswamy pull the Amy Acton mother ad?",
     "Will Vivek Ramaswamy pull the Amy Acton mother ad?"),
    ("Will Yemeni forces enter Sanaa by October 31?",
     "Yemeni forces enter Sanaa by...?"),
]
EMBARGOED_15 = {
    "ISM Manufacturing PMI - October 2026", "Reserve Bank of Australia Decision in November",
    "Bank of Brazil decision in November?", "Allstate (ALL) Q3 property-liability combined ratio?",
    "McDonald's (MCD) Q3 global comparable sales growth?", "Bank of England decision in November?",
    "Bank of Mexico Decision in November?", "Bank of England rate hike in 2026?", "How many jobs added in October?",
    "October Unemployment Rate", "October Inflation China - Annual", "October Inflation US - Monthly",
    "Core CPI MoM - October 2026", "Argentina Monthly Inflation - October", "UK GDP growth in Q3 2026 (QoQ)?",
}
FEATURED = (  # the main page's seven featured cases
    "US strikes Iran by February 28, 2026?",
    "Maduro out by January 31, 2026?",
    "Kristi Noem out by March 31?",
    "OpenAI browser in 2025?",
    "Supreme Court rules in favor of Trump's tariffs?",
    'Will "Tití Me Preguntó" be played first at the Super Bowl halftime show?',
    "Will Michael B. Jordan win Best Actor at the 98th Academy Awards?",
)
BET = re.compile(r"\bbet(s|ting|tor|tors|ted)?\b|\bwager|\bgambl|\bodds\b", re.I)


def test_embargo_old_replay():
    assert len(REPLAY_EVENTS) == 201
    hits = {e for q, e in REPLAY_EVENTS if K.is_embargoed(q, e)}
    # the 15, and Anthropic's offering: scheduled since its roadshow began (kinds.SCHEDULED_OFFERINGS), so its
    # date markets are left out as the proposal's table has it
    anthropic = {"In which month will Anthropic IPO?", "Anthropic IPO by __?"}
    assert hits == EMBARGOED_15 | anthropic, (hits - EMBARGOED_15, EMBARGOED_15 - hits)
    assert K.is_embargoed("Will Core CPI MoM be 0.3% in October?", "Core CPI MoM - October 2026") == "official statistic"
    assert K.is_embargoed("Bank of Mexico decreases interest rates by 25 bps after November 2026 meeting?",
                          "Bank of Mexico Decision in November?") == "central bank rate decision"
    assert K.is_embargoed("McDonald's (MCD) Q3 global comparable sales growth 1% to 2%?") == "scheduled company results"


def test_embargo_spares_the_featured_cases_and_any_day_picks():
    for q in FEATURED:
        assert K.is_embargoed(q) is None, q
    for q, e in (("Lisa Cook officially out as Fed Governor by December 31?", "Lisa Cook officially out as Fed Governor"),
                 ("Will Trump nominate Kevin Warsh as the next Fed chair?", "Who will Trump nominate as Fed Chair?"),
                 ("Kraken IPO by December 31, 2026?", "Kraken IPO by ___ ?"),
                 # whether an offering happens at all is a company decision; its price is not
                 ("Will Holtec Nuclear not IPO before November 2026?", "Holtec Nuclear IPO Closing Market Cap"),
                 ("Will Tesla cut 10% of jobs in 2026?", "Tesla layoffs")):
        assert K.is_embargoed(q, e) is None, q
    # emergency moves and offering prices are embargoed too (decision 2, 2026-11-13)
    assert K.is_embargoed("Fed emergency rate cut before 2027?") == "central bank rate decision"
    assert K.is_embargoed("Will the People’s Bank of China not change rates by December 31?") == "central bank rate decision"
    assert K.is_embargoed("Will OpenAI's market cap be less than $500B at market close on IPO day?") == "scheduled share offering"
    assert K.is_embargoed("Will Anthropic raise less than $65B in its IPO?") == "scheduled share offering"


def test_closed_exclusion_after_the_factual_changes():
    assert K.closed_exclusion("Will monthly inflation increase by 0.4% in October?", "October Inflation US - Monthly") \
        == K.EMBARGO_REASON
    # a company's own year-end list is no longer a public count, though the old count rule's words match it
    wrapped = "Will Taylor Swift be Spotify Wrapped's top artist of 2026?"
    assert K.closed_exclusion(wrapped) is None and up.PUBLIC_COUNT.search(wrapped)
    assert K.closed_exclusion("Will Charlie Kirk be a top 5 most searched person on Google in 2026?") is None
    # the words of a scripted, scheduled speech are no longer a public-speech market
    sotu = 'Will Trump say "tariff" during the State of the Union?'
    assert K.closed_exclusion(sotu) is None and up.MENTION.search(sotu)
    assert K.closed_exclusion('Will Trump say "Bibi" this week?') == "words said in public (mention market)"
    assert K.closed_exclusion("Will MrBeast's next video get at least 100 million views on week 1?").startswith("counted")
    assert K.closed_exclusion("Will the Democrats win the Ohio Senate race in 2026?") == "election or vote"
    assert K.closed_exclusion("Will there be between 35 and 59 tornadoes in the US in October 2026?") == "weather"
    for q in FEATURED:
        assert K.closed_exclusion(q) is None, q


def test_upsets_uses_the_shared_rules():
    for name in ("PUBLIC_COUNT", "MENTION", "NATURE", "ELECTION", "CONTINUES", "NEGATED", "happened_side"):
        assert getattr(up, name) is getattr(K, name), name
    assert K.happened_side("US x Iran ceasefire continues through November 8?") == "no"
    assert K.happened_side("Will there be no next Google Gemini Pro model release by November 1, 2026?") == "no"
    assert K.happened_side("Maduro out by January 31, 2026?") == "yes"


def _filed(title, question=""):
    return K.file_event(title, question or title)


def test_corrected_filing():
    # 1. Israel-Iran and Israel-Hamas are different events; 5. "continues through" is filed by the strike that ends it
    f = _filed("Israel x Iran ceasefire continues through...?", "Israel x Iran ceasefire continues through November 13?")
    assert (f.kind, f.group, f.place) == ("planned_strike_or_op", 1, "any_day")
    assert _filed("Israel x Hamas ceasefire cancelled by...?", "Israel x Hamas ceasefire cancelled by December 31?").kind \
        == "ceasefire_peace"
    # 2. picks made by one leader or a small body are personnel picks, not elections
    for t, q in (("Democratic VP Nominee 2028", "Will Alexandria Ocasio-Cortez be the 2028 Democratic Vice-Presidential nominee?"),
                 ("Who will be the next Secretary-General of the UN?",
                  "Will Michelle Bachelet be the next Secretary General of the United Nations?"),
                 ("Next Israeli Minister of Defense?", "Will Naftali Bennett be the next Israeli Minister of Defense?"),
                 ("Next President of Germany?", "Will Ilse Aigner be the next President of Germany?"),
                 ("Next Press Secretary announced by...?", "Next White House Press Secretary announced by November 30, 2026?")):
        f = _filed(t, q)
        assert (f.kind, f.group, f.how) == ("personnel_pick", 1, "rule"), (t, f)
    # 3. joke markets are left out, so a passing remark is never a "step"
    for t in ("Trump renames ICE to NICE by...?", "Trump on $250 bill this year?",
              'Trump renames Strait of Hormuz to "Strait of Trump" by December 31?', "Will Trump rename an ocean by...?",
              "Trump renames New Mexico to New America by...?", "Will OpenAI rename itself to OpenSI?"):
        f = _filed(t)
        assert f.how == "excluded" and f.kind == "public speech or joke market", (t, f)
    # 4. Google's lists go to the dated place
    f = _filed("Top 5 Most Searched People on Google 2026", "Will Charlie Kirk be a top 5 most searched person on Google in 2026?")
    assert (f.kind, f.place) == ("company_list", "dated")
    # an index market is filed as an index inclusion, with or without "the", even when it names an IPO
    for t in ("SpaceX IPO: Officially added to S&P 500 by...?", "Strategy added to S&P 500 by...?"):
        assert _filed(t).kind == "index_inclusion", t
    assert _filed("Kraken IPO by...?").kind == "ipo_filing"
    # 6. no default kind: an unplaced market waits, in group 2
    f = _filed("Will Turkey move against the SDF by...?", "Will Turkey move against the SDF by December 31?")
    assert (f.kind, f.group, f.how) == (None, 2, "unfiled") and K.group_of(None) == 2
    # 7. mention markets about scripted, scheduled speeches go to the dated place; others are left out
    assert _filed("State of the Union mentions", 'Will Trump say "tariff" during the State of the Union?').kind == "scripted_words"
    assert _filed("What will Trump say this week?", 'Will Trump say "Bibi" this week?').how == "excluded"


def test_dated_place_and_exclusions():
    f = _filed("Eurovision Asia 2026: Jury Winner", "Will Malaysia be the Jury Winner in the Eurovision Asia 2026 Grand Final?")
    assert (f.kind, f.place) == ("jury_prize", "dated")
    for t, q in (("Eurovision Asia Winner 2026", "Will Malaysia win Eurovision Asia 2026?"),
                 ("Dancing with the Stars S35: Winner?", "Will Amber Glenn win Dancing With the Stars: Season 35?")):
        assert _filed(t, q).kind == "settled by a live public vote", t
    for t, kind in (("CMA New Artist of the Year 2026", "jury_prize"), ("Turner Prize 2026 Winner", "jury_prize"),
                    ("NCAA Football: 2026 Heisman Award Winner", "jury_prize"), ("Bachelorette Season 22 Winner", "taped_tv"),
                    ("TIME Person of the Year 2026", "magazine_pick"), ("Will Jason or Lucia die in GTA VI?", "finished_content"),
                    ("Which characters will appear in Avengers: Doomsday?", "finished_content"),
                    ("First song at 2027 Big Game Halftime Show?", "show_setlist")):
        f = _filed(t)
        assert (f.kind, f.place) == (kind, "dated"), (t, f)
    assert _filed('"Avengers: Doomsday" Rotten Tomatoes Score?').kind == "public count or ranking"
    # the seven featured cases
    want = ("planned_strike_or_op", "autocrat_or_regime", "us_official", "product_launch", "court_ruling",
            "show_setlist", "jury_prize")
    assert tuple(_filed(q).kind for q in FEATURED) == want
    assert [K.group_of(k) for k in want] == [1, 1, 1, 1, 2, 1, 1]
    # left out before any model call
    for t, why in (("Fed Decision in December?", "embargoed release: central bank rate decision"),
                   ("Will Russia capture Kostiantynivka by...?", "builds up in public"),
                   ("Pro Football: AFC East Champion", "game or sport"),
                   ("Hantavirus pandemic in 2026?", "nature or disease")):
        assert _filed(t).kind == why, t
    assert _filed("Hantavirus vaccine in 2026?").kind == "open_process"


def test_kinds_are_complete():
    assert {k.group for k in K.KINDS.values()} == {1, 2, 3}
    for k in K.KINDS.values():
        assert k.place in ("any_day", "dated") and k.section, k.key
        assert set(k.slots) <= set(K.SLOTS), k.key
        assert set(re.findall(r"\{(\w+)\}", k.question + k.settles + k.who_knows)) <= set(K.SLOTS) | {"people", "people_list"}
    rule_kinds = {kind for kind, _ in K.ANY_DAY_RULES} | {kind for kind, _ in K.DATED_RULES}
    assert rule_kinds <= set(K.KINDS) and K.DATED_KINDS == {kind for kind, _ in K.DATED_RULES}
    try:
        K.card_text("us_official", {"subject": "Howard Lutnick"})
        raise AssertionError("a missing name must raise")
    except ValueError as e:
        assert "post" in str(e)


def _all_names():
    return {"subject": "Alex Doe", "short": "Doe", "decider": "the board", "other": "Acme", "post": "the post",
            "what": "the thing", "which": "Who", "question": "Will it happen?", "others": ["Sam Roe"]}


def test_sentences_never_say_bet_or_odds():
    for key, k in K.KINDS.items():
        for raw in (k.question, k.settles, k.who_knows, k.why):
            assert not BET.search(raw), (key, raw)
        t = K.card_text(key, _all_names())
        for field, s in t.items():
            assert not BET.search(s), (key, field, s)
            assert "{" not in s and "}" not in s, (key, field, s)
        assert re.search(r"\bknows?\b", t["who_knows"]), (key, t["who_knows"])
        assert t["why_watched"].endswith("."), key
    for name in dir(K):
        if name.startswith("_CASE_"):
            assert not BET.search(getattr(K, name)), name


def test_the_proposals_cards():
    lut = K.card_text("us_official", {"subject": "Howard Lutnick", "short": "Lutnick", "post": "the Commerce Department",
                                      "decider": "Trump"})
    assert lut["question"] == "Will Howard Lutnick leave the Commerce Department?"
    assert lut["who_knows"] == "If Lutnick goes, Trump, Lutnick and a few aides will know before the public does."
    assert "Biden's pardons" in lut["why_watched"] and "NPR" in lut["why_watched"]
    ts = K.card_text("merger_acquisition", {"subject": "Tesla and SpaceX", "decider": "Elon Musk"})
    assert ts["question"] == "Will Tesla and SpaceX announce a merger?"
    assert ts["who_knows"] == "If it happens, Elon Musk, both boards and their bankers will know before the public does."
    nv = K.card_text("company_exec_pick", {"subject": "Nvidia", "what": "a permanent chief executive", "decider": "Nvidia's board"})
    assert nv["question"] == "Will Nvidia name a permanent chief executive?"
    assert nv["who_knows"] == "If it happens, Nvidia's board and the person chosen will know before the public does."
    ru = K.card_text("us_official", {"subject": "Marco Rubio", "short": "Rubio", "post": "the State Department",
                                     "decider": "Rubio", "others": ["Trump"]})
    assert ru["who_knows"] == "If Rubio goes, Rubio, Trump and a few aides will know before the public does."
    eu = K.card_text("jury_prize", {"which": "Which country", "what": "the Eurovision Asia jury vote",
                                    "decider": "the Eurovision Asia Grand Final"})
    assert eu["question"] == "Which country wins the Eurovision Asia jury vote?"
    # a dated kind's sentence names no one the model wrote: an event never "knows"
    assert eu["who_knows"] == ("The votes are cast before the result is announced, so the jurors and the vote "
                               "counters will know it first.")
    st = K.card_text("planned_strike_or_op", {"subject": "Israel or Iran", "what": "a strike on the other",
                                              "decider": "Israel's and Iran's leaders"})
    assert st["question"] == "Will Israel or Iran carry out a strike on the other?"
    assert st["settles"] == "Israel or Iran carries out a strike on the other"
    # a name said twice is said once: a short form inside the full name, or the template's own words
    tr = K.card_text("us_executive", {"subject": "Donald Trump", "short": "Trump", "what": "new tariffs"})
    assert tr["who_knows"] == "If it happens, Donald Trump and a few aides will know before the public does."
    bond = K.card_text("casting", {"post": "James Bond", "decider": "the producers"})
    assert bond["who_knows"] == ("Until it is announced, the producers and the actors' agents will know the choice "
                                 "before the public does.")


def test_odds_and_betting_headlines():
    for title, source in (("Nvidia executive Ian Buck leads CEO succession odds at 35.5%", "Benzinga"),
                          ("White House press secretary odds: Ben Ferguson increases to 25%", "Kalshi News"),
                          ("Howard Lutnick's departure odds hit 47% as Jensen Huang exits Nvidia", "Kalshi News"),
                          ("Who will Trump pick next?", "Kalshi News"),
                          ("Polymarket bettors pile into Fed chair market", "CoinDesk"),
                          ("Fox News Reveals CNN Pundit Is ‘Frontrunner’ for Trump’s Press Secretary Role", "Mediaite"),
                          ("New Favorite for Trump’s Press Secretary Emerges After White House Leak", "Yahoo"),
                          ("Bookies slash odds on Rubio exit", "The Sun"),
                          ("Ferguson now at 40% to be named press secretary", "Covers")):
        assert K.is_odds(title, source), title
    for title, source in (("Trump Pauses 50% Tariffs on Canada as Trade Talks Advance", "EnergyNow"),
                          ("SpaceX and Tesla Talks on Record Merger Are Said to Progress", "Bloomberg"),
                          ("US to take 10% stake in Intel", "Reuters"),
                          ("Tesla to cut 10% of jobs worldwide", "Reuters"),
                          ("Israeli security cabinet orders military to prepare for possible attack on Iran: Report",
                           "Anadolu Agency"),
                          ("Marco Rubio Reportedly Considers Resigning as Secretary of State", "BetaNews")):
        assert not K.is_odds(title, source), title


def test_step_and_leak_words():
    assert K.has_step_words("Israeli security cabinet orders military to prepare for possible attack on Iran: Report")
    assert K.has_step_words("Variational launches on mainnet ahead of $VAR token event")
    assert K.has_step_words("Stripe, Advent negotiate higher PayPal bid after $53 billion offer rejected")
    assert not K.has_step_words("Lutnick's longtime friendship with Trump may not save him")
    assert K.is_leak("White House discussing Howard Lutnick's future as Commerce secretary, people familiar say")
    assert K.is_leak("SpaceX and Tesla Talks on Record Merger Are Said to Progress")
    assert K.is_leak("EXCLUSIVE: US strikes on Iran could target individual leaders, officials say")
    assert not K.is_leak("Trump Says He Backs the Idea of a Ban on Diesel Exports")
    assert not K.is_leak("Nvidia Drama Triggers Difficult Search for Huang’s Successor")


def test_sources_and_the_card_headline():
    assert [K.source_rank(s) for s in ("Reuters", "Bloomberg.com", "AP News", "Anadolu Agency", "AFP")] == [0] * 5
    assert [K.source_rank(s) for s in ("WSJ", "The New York Times", "BBC", "Axios", "Politico")] == [1] * 5
    assert [K.source_rank(s) for s in ("Democracy Now!", "Crypto Briefing", "Yahoo", "Reuters Tumblr")] == [2] * 4
    steps = [{"title": "a", "source": "WSJ", "published": "2026-11-13T00:44:00Z"},
             {"title": "b", "source": "Reuters", "published": "2026-11-12T18:19:11Z"},
             {"title": "c", "source": "The Telegraph", "published": "2026-11-12T21:36:00Z"},
             {"title": "d", "source": "Reuters", "published": "2026-11-12T10:00:00Z"}]
    assert K.card_headline(steps)["title"] == "b" and K.card_headline([]) is None
    assert K.card_headline(steps[:1] + steps[2:3])["title"] == "a"


def test_times_and_copies():
    assert K.is_date_only("2026-10-22T07:00:00Z") and K.is_date_only("2026-11-04T08:00:00+00:00")
    assert not K.is_date_only("2026-11-12T18:19:11Z")
    assert K.effective_ts("2026-10-22T07:00:00Z") == 1792713540            # 2026-10-22 23:59 UTC
    assert K.effective_ts("2026-11-12T18:19:11Z") == 1794507551 and K.effective_ts("") is None
    given = [{"title": "Trump ‘considering sacking Lutnick’ in wake of Nvidia scandal", "source": "The Telegraph"},
             {"title": "Lutnick Fights for His Job as Trump Aides Blame Him for Nvidia Fallout", "source": "WSJ"}]
    assert K.match_copy("Trump 'considering sacking Lutnick' in wake of Nvidia scandal", given) is given[0]
    assert K.match_copy("lutnick fights for his job as trump aides blame him for nvidia fallout - WSJ", given) is given[1]
    assert K.match_copy("Trump considering sacking Lutnick", given) is None
    assert K.match_copy("none", given) is None and K.match_copy("", given) is None
    lut = {"subject": "Howard Lutnick", "short": "Lutnick"}
    assert K.names_subject(given[0]["title"], lut) and not K.names_subject("Commerce secretary faces probe", lut)
    assert K.names_subject("SpaceX and Tesla Talks on Record Merger Are Said to Progress", {"subject": "Tesla and SpaceX"})
    assert K.names_subject("NVIDIA board meets", {"subject": "Nvidia"})
    assert not K.names_subject("Nvidiaesque", {"subject": "Nvidia"})


def test_embargo_is_about_the_release_itself():
    # a bank or a meeting named in passing is not a rate decision; a law named "inflation" is not a statistic
    for q in ("Will Trump fire Powell before the December Fed meeting?", "Will Trump cap credit card interest rates at 10%?",
              "Will the Fed cut staff by 10%?", "Will the Inflation Reduction Act be repealed?",
              "Lisa Cook officially out as Fed Governor by December 31?"):
        assert K.is_embargoed(q) is None, q
    for q in ("Fed decision in December?", "Will the Fed cut in December?", "How many Fed rate cuts in 2026?",
              "Bank of Japan decision in January?", "Will the ECB hike in 2026?", "Will the Fed pause?"):
        assert K.is_embargoed(q) == "central bank rate decision", q
    assert K.is_embargoed("Will US inflation be above 3% in November?") == "official statistic"
    assert K.is_embargoed("Lead Bank in Anthropic's IPO?") == "scheduled share offering"
    assert K.is_embargoed("Kraken IPO by December 31, 2026?", "Kraken IPO by ___ ?") is None
    # an election combined with a statistic is an election (both lists leave it out; the closed list says why)
    assert K.closed_exclusion("Will the Democrats fail to sweep the House and Senate and the US enter a recession by "
                              "the end of 2027?") == "election or vote"
    assert K.closed_exclusion("Will the Democrats sweep the House and Senate and the US-Iran ceasefire hold through "
                              "2027?") == "election or vote"


def test_headline_must_name_the_story():
    sbf = K.match_names("pardon_or_release", {"subject": "SBF", "short": "SBF", "search_event": "SBF released from custody"})
    assert not K.names_subject("Soitec shares top the SBF 120 but remain below their MM200", sbf)   # a stock index
    assert K.names_subject("SBF released from prison after commutation", sbf)
    assert K.names_subject("Sam Bankman-Fried pardoned", K.match_names("pardon_or_release", {"subject": "Sam Bankman-Fried"}))
    assert K.subject_terms({"subject": "Sam Bankman-Fried"}) == ["Sam Bankman-Fried", "Sam Bankman-Fried"]  # no "Fried"
    cab = K.match_names("product_launch", {"subject": "Tesla", "what": "a Cybercab for 30k or less"},
                        "Will Tesla sell a Cybercab for 30k or less in 2026?")
    assert cab["require"] == ["Cybercab"]
    assert not K.names_subject("SpaceX and Tesla Talks on Record Merger Are Said to Progress", cab)
    assert K.names_subject("Tesla starts Cybercab production in Texas", cab)
    # a capitalised name written all in lower case is a common word, not the name
    assert not K.names_subject("Zelensky says Europe to join talks as discord persists", {"subject": "Discord"})
    assert K.names_subject("DISCORD FILES FOR IPO", {"subject": "Discord"})
    # a kind that names who acts needs no outcome word
    assert "require" not in K.match_names("us_official", {"subject": "Howard Lutnick", "post": "the Commerce Department"})


def test_story_merges_and_deciders():
    assert K.STORY_MERGES["1367877"] == K.STORY_MERGES["1353337"] == "next nvidia ceo"
    # final.md correction 1: the Israel x Hamas ceasefire is not the Israel x Iran story
    assert "57057" not in K.STORY_MERGES and "711714" not in K.STORY_MERGES
    al = K.DECIDER_ALIASES["trump"]
    assert {"the president", "donald trump", "the white house"} <= al


if __name__ == "__main__":
    checks = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = []
    for fn in checks:
        try:
            fn()
            print(f"PASS {fn.__name__}", flush=True)
        except Exception as e:
            failed.append(fn.__name__)
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {e}", flush=True)
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    sys.exit(1 if failed else 0)
