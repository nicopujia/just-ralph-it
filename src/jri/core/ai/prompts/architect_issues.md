<!-- JRI appends this to the architect instructions on every cycle except the last. -->
Return `functional_specification_issues` instead of `architecture` when the functional specifications contradict themselves, omit behavior required for implementation, or leave a behavioral choice to the implementer. Report every one, not only the first, because each set you return costs a full re-analysis.
