import type { ReactNode } from "react";
import { href } from "../lib/router";
import "../components/ui.css";
import "../components/shell.css";

export function NotFound({ title = "This page doesn’t exist", children }: { title?: string; children?: ReactNode }) {
  return (
    <section className="container notfound" aria-labelledby="notfound-title">
      <div className="notfound-code gradient-text" aria-hidden>
        404
      </div>
      <h1 id="notfound-title">{title}</h1>
      <p>{children ?? "The link may be mistyped, or the page moved. Every market is listed on the home page."}</p>
      <a className="btn btn-solid" href={href.home()}>
        All markets
      </a>
    </section>
  );
}
