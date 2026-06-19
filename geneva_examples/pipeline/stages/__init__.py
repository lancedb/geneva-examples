"""Geneva backfill stages, each with its own CLI entrypoint.

Image table (`images`.`image`):
- ``geneva_examples.pipeline.stages.lightweight`` — file size + image dimensions
- ``geneva_examples.pipeline.stages.embeddings`` — OpenCLIP image embeddings
- ``geneva_examples.pipeline.stages.captions`` — BLIP captions (two UDF variants)

Video frames (`video_clips`.`frame`):
- ``geneva_examples.pipeline.stages.frame_embed`` — OpenCLIP embeddings
- ``geneva_examples.pipeline.stages.frame_caption`` — BLIP captions
- ``geneva_examples.pipeline.stages.frame_openpose`` — OpenPose pose-skeleton PNGs

The UDF bodies live in ``geneva_examples.udfs``; ``_runner.backfill_column`` holds the
shared add-column/backfill flow. Import concrete CLIs from their submodules so
importing this package does not eagerly pull in heavy dependencies.
"""
