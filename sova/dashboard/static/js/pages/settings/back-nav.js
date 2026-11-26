/* Opening a section and going back to the Settings home (the phone views, #1566).

   A section opened from the home view goes back with the browser's own history, so a later Back (or the swipe) does
   not return to the section; any other way into a section leaves no home entry to go back to, so Back pushes one.
   The provenance lives on the section's history entry (history.state), so Back then Forward restores it.

   The browser objects and the page's two actions come in as arguments, so the module runs against a simulated
   history (tests/test_settings_back_nav.py). `route()` opens what the hash names, `isHomeView()` says whether the
   page shows the home view and `scrollTop()` returns to the top after Back opens it. */

export const FROM_HOME = 'settingsFromHome';

export function createBackNav({ history, location, route, isHomeView, scrollTop }) {
  function enteredFromHome() {
    return !!(history.state && history.state[FROM_HOME]);
  }

  /* The Back control of a section view. */
  function goHome() {
    if (enteredFromHome()) {
      history.back();
      return;
    }
    history.pushState(null, '', location.pathname + location.search);
    route();
    scrollTop();
  }

  /* Follows a link to `hash`. */
  function go(hash) {
    const fromHome = !!(hash && isHomeView());
    // Following the same hash again (a second click on the current section) fires no hashchange.
    if (location.hash === '#' + hash) route();
    else if (fromHome) {
      // pushState fires no hashchange, so the section opens by hand and its entry records where it came from.
      history.pushState({ [FROM_HOME]: true }, '', '#' + hash);
      route();
    } else location.hash = hash;
  }

  return { goHome, go };
}
