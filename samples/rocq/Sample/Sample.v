(* Minimal Rocq project for Choir's verify pipeline. `one_add_one` is left
   Admitted — the canonical Choir-task target: a PR replaces `Admitted` with a
   real proof (`Qed`), and verify-pr rebuilds clean. Not a serious artefact. *)

Theorem zero_add_zero : 0 + 0 = 0.
Proof. reflexivity. Qed.

Theorem one_add_one : 1 + 1 = 2.
Proof.
Admitted.
