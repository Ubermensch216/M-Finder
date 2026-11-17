//! Offline stub for `ureq` so M-Finder's preview binary has zero TLS/network dependencies.

use std::time::Duration;

pub struct Agent;
pub struct ConfigBuilder;
pub struct RequestBuilder;
pub struct Response;
pub struct Body;
pub struct BodyReader;

impl Agent {
    pub fn config_builder() -> ConfigBuilder {
        ConfigBuilder
    }
    pub fn get(&self, _url: &str) -> RequestBuilder {
        RequestBuilder
    }
}

impl ConfigBuilder {
    pub fn timeout_global(self, _d: Option<Duration>) -> Self {
        self
    }
    pub fn max_redirects(self, _n: u32) -> Self {
        self
    }
    pub fn build(self) -> Self {
        self
    }
}

impl From<ConfigBuilder> for Agent {
    fn from(_: ConfigBuilder) -> Self {
        Agent
    }
}

impl RequestBuilder {
    pub fn call(self) -> Result<Response, String> {
        Err("offline".into())
    }
}

impl Response {
    pub fn body_mut(&mut self) -> &mut Body {
        unreachable!()
    }
}

impl Body {
    pub fn with_config(&mut self) -> BodyReader {
        BodyReader
    }
}

impl BodyReader {
    pub fn limit(self, _n: u64) -> Self {
        self
    }
    pub fn read_to_vec(self) -> Result<Vec<u8>, String> {
        Err("offline".into())
    }
}
