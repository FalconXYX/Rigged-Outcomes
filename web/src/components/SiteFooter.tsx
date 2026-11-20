import { BrandMark, GitHubIcon, REPO_URL, SITE_URL } from "./SiteHeader";
import { useRoute } from "../lib/router";
import "./shell.css";

/**
 * One ruled strip: brand · data note · author + repo. The data note is about
 * the market pages' data (times in ET); the watchlist reads the news as well
 * as Polymarket and gives its own sources, so it leaves it out.
 */
export function SiteFooter() {
  const stats = useRoute().page !== "watchlist";
  return (
    <footer className="site-footer">
      <div className="container site-footer-inner">
        <span className="footer-brand">
          <BrandMark size={18} />
          Rigged Outcomes
        </span>
        <div className="footer-meta">
          {stats && <span>Data: Polymarket Data API v2 + Gamma · times ET</span>}
          <span className="footer-links">
            <span>
              Built by{" "}
              <a href={SITE_URL} className="footer-link">
                Parth Jain
              </a>
            </span>
            <span className="footer-sep" aria-hidden>
              ·
            </span>
            <a className="footer-link footer-gh" href={REPO_URL} target="_blank" rel="noopener noreferrer">
              <GitHubIcon size={13} />
              GitHub
            </a>
          </span>
        </div>
      </div>
    </footer>
  );
}
