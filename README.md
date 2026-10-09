<p align="center">
  <img width="832" height="480" alt="2026-10-09-03h11m20s_seed93365271_fix the characters hat and make sure it says dyva clean up the compression artifacts and make the" src="https://github.com/user-attachments/assets/541d3805-67e3-4aeb-86ba-9d48d493d540" />
<br/>
  <a href=https://pypi.org/project/dyva><img src=https://badge.fury.io/py/dyva.svg/></a>
  <a href=https://pepy.tech/projects/dyva><img src=https://static.pepy.tech/badge/dyva/week></a><br/> 
 <br/><b>Try it now</b><br/>
  <code>uvx dyva</code><br/>
</p>

---
**Dyva** is a proxy that transparently routes to insecure [Ollama](https://ollama.com/), [vLLM](https://github.com/vllm-project/vllm), [LM Studio](https://lmstudio.ai/), [LocalAI](https://localai.io/), [SGLang](https://github.com/sgl-project/sglang), [KTransformers](https://github.com/kvcache-ai/ktransformers), [MLX](https://github.com/ml-explore/mlx), [llama.cpp](https://github.com/ggml-org/llama.cpp), [ds4 (DwarfStar)](https://github.com/antirez/ds4), [Fooocus](https://github.com/lllyasviel/Fooocus), [Strata](https://github.com/Niko1221/Strata), [A1111 (stable-diffusion-webui)](https://github.com/AUTOMATIC1111/stable-diffusion-webui), [Gradio](https://gradio.app/), and ComfyUI hosts from [Shodan](https://www.shodan.io/), [ZoomEye](https://www.zoomeye.org/), [Censys](https://search.censys.io/), [hunter.how](https://hunter.how/) and [FOFA](https://fofa.info/) with dynamic failover, context-window scaling, and honeypot detection. 

Capable of text generation, embeddings, decision models, vision, tool calling, image generation and editing, video generatoin, TTS, and music generation in a unified interface with state of the art routing providing sub 1s latency. 

Try it now [by clicking here](https://9ol.es/11434) or run it yourself with a few keystrokes on Linux, Windows, Mac, and even Android. 


```
uvx dyva
```

## Querying for models
The massive varieties of models and installations are unified through a simple model query syntax.

This is designed so you can slice and dice the pool in any application. Place it AS the model.

For instance `*` means use any model. `>2026` means anything released after 2026, `qwen>2026>10gb,gemma4` means 'use any qwen released in 2026 over 10gb and fallback to Gemma4 if you can't find any"

The details:

* Specified as partial strings or globs such as `qwen*27b` or even `abliterated` for the times you want to slip into something more comfortable.
* As fallbacks with a comma such as `gemma3,qwen3.6` (try the first, fall back to the second)
* By size like `qwen >10gb`
* By release date like `qwen>2026`.

They can be stacked so `qwen>2026>5gb` means the newer large qwens. [More details](https://github.com/kristopolous/free-ollama/tree/main/dyva#model-names-are-routing-patterns)

## Finding new hosts
You can multi source host lists or bring your own. There's a separate survey tool called [graflex](graflex).

That means [dyva](dyva) can sit on top of your own infrastructure as well. 

It is a feature-rich system with chat that includes image generatoin, web search, sub agent, documents, video generation, tts, stats, and even graphically tweaking the routing algorithm.


<img alt="sshot" src="https://github.com/user-attachments/assets/5afdf551-d62f-4146-a4b6-9e0bdc9ac4bc" />


There's also a [simple command line](freeollama.md).

```
Pet the feral llama

   \\         
    l'> Bahhhhh
    ll       
    llama~  
    || ||  
    '' ''
```
