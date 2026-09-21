theory Sample
  imports Main
begin

(* Minimal Isabelle/HOL project for Choir's verify pipeline. The `sorry` on
   `one_add_one` is the canonical Choir-task target: a PR replaces it with a
   real proof and verify-pr rebuilds the session clean. Not a serious artefact. *)

lemma zero_add_zero: "(0::nat) + 0 = 0"
  by simp

lemma one_add_one: "(1::nat) + 1 = 2"
  sorry

end
