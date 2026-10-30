import styles from "./diagram.module.css";

/** The two highlight classes, one per keyframes name, alternated so each frame restarts. */
export const FLASH = [styles.flashA, styles.flashB] as const;
