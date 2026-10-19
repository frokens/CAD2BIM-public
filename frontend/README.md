# CAD2BIM workbench

Current browser workbench and backend proxy source. Historical conversation replays, CAD/model snapshots,
saved resource instances and private hosting IDs are excluded from this distribution.

Follow the root README to prepare the local backend and load its generated `frontend.env` before
running `npm ci` and `npm run dev`. Select **新建任务 → Component**; there is intentionally no archived project to replay.
The resource library lists resources in your own backend and is initially empty without registration.

`npm run build` builds the browser source. `npm test` runs the public proxy checks after building;
the old screenshot/replay assertions depend on private samples and are not distributed.
