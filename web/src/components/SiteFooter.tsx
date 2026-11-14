import { BrandMark, GitHubIcon, REPO_URL, SITE_URL } from "./SiteHeader";
import { useRoute } from "../lib/router";
import "./shell.css";

/**
 * One ruled strip: brand · caveat · data note · author + repo. The caveat and
 * the data note are about the market pages' statistics (times in ET); the
 * watchlist has no statistics and gives its times in UTC, so it leaves them out.
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
          {stats && (
            <>
              <span>Flags are statistical signals, not proof of wrongdoing.</span>
              <span>Data: Polymarket Data API v2 + Gamma · times ET</span>
            </>
          )}
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
