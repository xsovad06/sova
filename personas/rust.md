# Rust Persona

> Auto-detected when: `Cargo.toml` exists

## Architecture

- Organize by module, use `mod.rs` or inline modules
- Keep `main.rs` / `lib.rs` thin — delegate to modules
- Use workspace for multi-crate projects
- Separate library logic from binary entry points

## Ownership & Borrowing

- Prefer borrowing (`&T`) over ownership when function doesn't need to own
- Use `&str` in function parameters, not `String`
- Use `Cow<'_, str>` when you might or might not need to allocate
- Clone explicitly when needed — don't fight the borrow checker with unsafe

## Error Handling

- Define domain error types with `thiserror`
- Use `anyhow` for application code, `thiserror` for library code
- Use `?` for propagation, not `.unwrap()` in production code
- `.unwrap()` is acceptable in tests and when invariant is documented
- Map errors at boundaries: `map_err(|e| MyError::from(e))`

## Traits & Generics

- Use traits for polymorphism and dependency injection
- Prefer `impl Trait` in function signatures over explicit generics when simple
- Use `dyn Trait` for runtime polymorphism (trait objects)
- Derive common traits: `Debug`, `Clone`, `PartialEq`, `Eq`, `Hash` as appropriate

## Patterns

- Use the builder pattern for complex struct construction
- Use the newtype pattern for type safety (`struct UserId(u64)`)
- Use enums with data for state machines
- Prefer iterators and combinators over manual loops
- Use `Option` and `Result` combinators (`.map()`, `.and_then()`, `.unwrap_or_default()`)

## Concurrency

- Use `tokio` for async runtime (or `async-std` if project convention)
- Use `Arc<Mutex<T>>` sparingly — prefer message passing (channels)
- Use `RwLock` when reads vastly outnumber writes
- Don't hold locks across `.await` points

## Testing

- Unit tests in the same file: `#[cfg(test)] mod tests`
- Integration tests in `tests/` directory
- Use `#[should_panic]` for expected panics
- Use `assert_eq!` with descriptive messages
- Use `mockall` or manual mocks for trait-based dependencies
- Test error paths, not just happy paths

## Dependencies

- Audit dependencies: `cargo audit`
- Use `cargo clippy` — treat warnings as errors in CI
- Use `cargo fmt` — enforce consistent formatting
- Pin dependency versions in `Cargo.lock` (commit it for binaries)

## Common Pitfalls

- Don't use `String` when `&str` suffices
- Don't use `.clone()` to silence the borrow checker — understand the ownership
- Don't use `unsafe` without a safety comment explaining the invariant
- Don't ignore `#[must_use]` return values (especially `Result`)
- Don't use `println!` for logging — use `tracing` or `log`
