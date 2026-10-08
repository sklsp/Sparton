// Moved out of og-card.html so the Content-Security-Policy can forbid inline scripts.
import { flapWord } from "/app/board.js";
document.getElementById("brand").append(flapWord("SPARTON"));
document.getElementById("l1").append(flapWord("WHAT YOUR RIVALS"));
document.getElementById("l2").append(flapWord("CHANGED THIS WEEK"));
